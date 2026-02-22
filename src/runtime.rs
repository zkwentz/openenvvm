//! MicroVM runtime management

use crate::error::{MicroVMError, Result};
use serde::Serialize;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;
use tokio::process::{Child, Command};
use tokio::sync::Mutex;
use tokio::time::{sleep, timeout};

/// Represents a running MicroVM instance
#[derive(Debug)]
#[allow(dead_code)]
pub struct MicroVM {
    pub vm_id: String,
    process: Arc<Mutex<Child>>,
    pub socket_path: PathBuf,
    pub tap_device: String,
    pub ip_address: String,
    pub port: u16,
}

impl MicroVM {
    /// Get the HTTP URL for this VM
    pub fn url(&self) -> String {
        format!("http://{}:{}", self.ip_address, self.port)
    }

    /// Check if the VM is responding to health checks
    pub async fn is_healthy(&self) -> bool {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(1))
            .build();

        match client {
            Ok(client) => {
                let url = format!("{}/health", self.url());
                client.get(&url).send().await.is_ok()
            }
            Err(_) => false,
        }
    }

    /// Stop the MicroVM gracefully
    pub async fn stop(&self) -> Result<()> {
        let mut process = self.process.lock().await;

        // Try graceful termination first
        if let Some(id) = process.id() {
            // Send SIGTERM
            #[cfg(unix)]
            {
                use nix::sys::signal::{kill, Signal};
                use nix::unistd::Pid;
                let _ = kill(Pid::from_raw(id as i32), Signal::SIGTERM);
            }

            // Wait with timeout
            match timeout(Duration::from_secs(5), process.wait()).await {
                Ok(_) => {}
                Err(_) => {
                    // Force kill if graceful shutdown failed
                    let _ = process.kill().await;
                }
            }
        }

        // Cleanup TAP device
        let _ = Command::new("sudo")
            .args(["ip", "link", "delete", &self.tap_device])
            .output()
            .await;

        // Cleanup socket
        if self.socket_path.exists() {
            let _ = tokio::fs::remove_file(&self.socket_path).await;
        }

        Ok(())
    }
}

/// Request body for Firecracker API actions
#[derive(Debug, Serialize)]
struct ActionRequest {
    action_type: String,
}

/// Start a MicroVM from a package
///
/// # Arguments
/// * `package_path` - Path to the .microvm package directory
/// * `vm_id` - Optional VM identifier (generated if not provided)
/// * `port` - Port to expose the environment on
/// * `ip_address` - IP address for the VM
/// * `gateway` - Gateway IP for the VM
///
/// # Returns
/// MicroVM instance representing the running VM
pub async fn start_microvm(
    package_path: &Path,
    vm_id: Option<&str>,
    port: u16,
    ip_address: &str,
    gateway: &str,
) -> Result<MicroVM> {
    let package = package_path.to_path_buf();

    let vm_id = vm_id
        .map(|s| s.to_string())
        .unwrap_or_else(|| format!("vm-{}-{}", std::process::id(), timestamp_millis()));

    // Setup networking
    let tap_device = format!("tap-{}", &vm_id[..vm_id.len().min(8)]);
    setup_networking(&tap_device, gateway).await?;

    // Create API socket path
    let socket_path = PathBuf::from(format!("/tmp/firecracker-{}.socket", vm_id));
    if socket_path.exists() {
        tokio::fs::remove_file(&socket_path).await?;
    }

    // Start Firecracker
    let config_path = package.join("config.json");
    let process = Command::new("firecracker")
        .args([
            "--api-sock",
            socket_path.to_str().unwrap(),
            "--config-file",
            config_path.to_str().unwrap(),
        ])
        .current_dir(&package)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;

    // Wait for socket
    wait_for_socket(&socket_path, Duration::from_secs(10)).await?;

    // Configure and start the VM via API
    configure_vm(&socket_path).await?;

    let vm = MicroVM {
        vm_id,
        process: Arc::new(Mutex::new(process)),
        socket_path,
        tap_device,
        ip_address: ip_address.to_string(),
        port,
    };

    // Wait for VM to be healthy
    wait_for_healthy(&vm, Duration::from_secs(30)).await?;

    Ok(vm)
}

/// Setup TAP device and networking for the VM
async fn setup_networking(tap_device: &str, gateway: &str) -> Result<()> {
    // Create TAP device
    run_sudo(&["ip", "tuntap", "add", "dev", tap_device, "mode", "tap"]).await?;

    // Configure TAP device
    let gateway_cidr = format!("{}/24", gateway);
    run_sudo(&["ip", "addr", "add", &gateway_cidr, "dev", tap_device]).await?;
    run_sudo(&["ip", "link", "set", tap_device, "up"]).await?;

    // Enable IP forwarding
    let _ = run_sudo(&["sysctl", "-w", "net.ipv4.ip_forward=1"]).await;

    // Setup NAT (ignore errors - might already be set up)
    let _ = run_sudo(&[
        "iptables",
        "-t",
        "nat",
        "-A",
        "POSTROUTING",
        "-o",
        "eth0",
        "-j",
        "MASQUERADE",
    ])
    .await;

    Ok(())
}

/// Run a command with sudo
async fn run_sudo(args: &[&str]) -> Result<()> {
    let output = Command::new("sudo").args(args).output().await?;

    if !output.status.success() {
        return Err(MicroVMError::CommandFailed {
            command: format!("sudo {}", args.join(" ")),
            message: String::from_utf8_lossy(&output.stderr).to_string(),
        });
    }

    Ok(())
}

/// Wait for the Firecracker API socket to be available
async fn wait_for_socket(socket_path: &Path, timeout_duration: Duration) -> Result<()> {
    let start = std::time::Instant::now();

    while start.elapsed() < timeout_duration {
        if socket_path.exists() {
            return Ok(());
        }
        sleep(Duration::from_millis(100)).await;
    }

    Err(MicroVMError::SocketNotAvailable(format!(
        "Socket not available after {:?}",
        timeout_duration
    )))
}

/// Configure the VM via Firecracker API
async fn configure_vm(socket_path: &Path) -> Result<()> {
    // Start the VM instance
    api_request(
        socket_path,
        "PUT",
        "/actions",
        &ActionRequest {
            action_type: "InstanceStart".to_string(),
        },
    )
    .await?;

    Ok(())
}

/// Make an API request to Firecracker via Unix socket
async fn api_request<T: Serialize>(
    socket_path: &Path,
    method: &str,
    path: &str,
    body: &T,
) -> Result<()> {
    use std::io::{Read, Write};
    use std::os::unix::net::UnixStream;

    let body_json = serde_json::to_string(body)?;

    let mut stream = UnixStream::connect(socket_path)?;
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    stream.set_write_timeout(Some(Duration::from_secs(5)))?;

    let request = format!(
        "{} {} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
        method,
        path,
        body_json.len(),
        body_json
    );

    stream.write_all(request.as_bytes())?;

    let mut response = String::new();
    let _ = stream.read_to_string(&mut response);

    // Check for HTTP error status
    if let Some(status_line) = response.lines().next() {
        if let Some(status) = status_line.split_whitespace().nth(1) {
            let status_code: u16 = status.parse().unwrap_or(0);
            if status_code >= 400 {
                return Err(MicroVMError::ApiError {
                    status: status_code,
                    message: response,
                });
            }
        }
    }

    Ok(())
}

/// Wait for the VM to pass health checks
async fn wait_for_healthy(vm: &MicroVM, timeout_duration: Duration) -> Result<()> {
    let start = std::time::Instant::now();

    while start.elapsed() < timeout_duration {
        if vm.is_healthy().await {
            return Ok(());
        }
        sleep(Duration::from_millis(500)).await;
    }

    Err(MicroVMError::HealthCheckFailed(
        timeout_duration.as_secs_f64(),
    ))
}

/// Get current timestamp in milliseconds
fn timestamp_millis() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_vm_url() {
        // We can't easily test MicroVM directly since it requires a process,
        // but we can test the URL generation logic
        let ip = "172.16.0.2";
        let port = 8000u16;
        let url = format!("http://{}:{}", ip, port);
        assert_eq!(url, "http://172.16.0.2:8000");
    }

    #[test]
    fn test_timestamp() {
        let ts = timestamp_millis();
        assert!(ts > 0);
    }

    #[test]
    fn test_action_request_serialization() {
        let req = ActionRequest {
            action_type: "InstanceStart".to_string(),
        };
        let json = serde_json::to_string(&req).unwrap();
        assert!(json.contains("InstanceStart"));
    }
}
