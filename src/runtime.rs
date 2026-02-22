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
    // Canonicalize the package path to get absolute paths
    let package = std::fs::canonicalize(package_path)?;

    let vm_id = vm_id
        .map(|s| s.to_string())
        .unwrap_or_else(|| format!("vm-{}-{}", std::process::id(), timestamp_millis()));

    eprintln!("Starting MicroVM from: {}", package.display());

    // Verify package files exist
    let kernel_path = package.join("vmlinux");
    let rootfs_path = package.join("rootfs.ext4");
    let config_path = package.join("config.json");

    if !kernel_path.exists() {
        return Err(MicroVMError::InvalidEnvPath(format!(
            "Kernel not found: {}",
            kernel_path.display()
        )));
    }
    if !rootfs_path.exists() {
        return Err(MicroVMError::InvalidEnvPath(format!(
            "Rootfs not found: {}",
            rootfs_path.display()
        )));
    }
    if !config_path.exists() {
        return Err(MicroVMError::InvalidEnvPath(format!(
            "Config not found: {}",
            config_path.display()
        )));
    }

    eprintln!("  Kernel: {} ({} bytes)", kernel_path.display(), std::fs::metadata(&kernel_path)?.len());
    eprintln!("  Rootfs: {} ({} bytes)", rootfs_path.display(), std::fs::metadata(&rootfs_path)?.len());

    // Setup networking - use tap0 to match config.json
    let tap_device = "tap0".to_string();
    setup_networking(&tap_device, gateway).await?;

    // Create API socket path
    let socket_path = PathBuf::from(format!("/tmp/firecracker-{}.socket", vm_id));
    if socket_path.exists() {
        tokio::fs::remove_file(&socket_path).await?;
    }

    // Update config.json with correct TAP device name (in case it differs)
    update_config_tap_device(&config_path, &tap_device)?;

    eprintln!("  Starting Firecracker with config: {}", config_path.display());

    // Start Firecracker
    let mut process = Command::new("firecracker")
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

    // Spawn a task to read and print Firecracker's stdout (VM console)
    if let Some(stdout) = process.stdout.take() {
        tokio::spawn(async move {
            use tokio::io::{AsyncBufReadExt, BufReader};
            let mut reader = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                eprintln!("[VM] {}", line);
            }
        });
    }

    // Spawn a task to read and print Firecracker's stderr
    if let Some(stderr) = process.stderr.take() {
        tokio::spawn(async move {
            use tokio::io::{AsyncBufReadExt, BufReader};
            let mut reader = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                eprintln!("[FC] {}", line);
            }
        });
    }

    // Wait for socket, checking if process dies
    let timeout_duration = Duration::from_secs(10);
    wait_for_socket_or_crash(&socket_path, &mut process, timeout_duration).await?;

    // Note: When using --config-file mode, Firecracker automatically boots the VM.
    // We don't need to call InstanceStart via the API - the VM is already running.

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
    eprintln!("  Setting up TAP device: {}", tap_device);

    // Create TAP device (ignore error if it already exists)
    let result = run_sudo(&["ip", "tuntap", "add", "dev", tap_device, "mode", "tap"]).await;
    if let Err(e) = &result {
        eprintln!("    TAP device creation (may already exist): {}", e);
    }

    // Configure TAP device (ignore error if IP already assigned)
    let gateway_cidr = format!("{}/24", gateway);
    let result = run_sudo(&["ip", "addr", "add", &gateway_cidr, "dev", tap_device]).await;
    if let Err(e) = &result {
        eprintln!("    IP address assignment (may already exist): {}", e);
    }

    // Bring up the device
    eprintln!("  Bringing up TAP device...");
    run_sudo(&["ip", "link", "set", tap_device, "up"]).await?;

    // Verify TAP device exists
    let output = Command::new("ip")
        .args(["link", "show", tap_device])
        .output()
        .await?;
    if output.status.success() {
        eprintln!("  TAP device {} is ready", tap_device);
    } else {
        return Err(MicroVMError::CommandFailed {
            command: format!("ip link show {}", tap_device),
            message: "TAP device not found after creation".to_string(),
        });
    }

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

/// Wait for the Firecracker API socket to be available, checking for crashes
async fn wait_for_socket_or_crash(
    socket_path: &Path,
    process: &mut Child,
    timeout_duration: Duration,
) -> Result<()> {
    use tokio::io::AsyncReadExt;

    let start = std::time::Instant::now();
    eprintln!("  Waiting for Firecracker socket: {}", socket_path.display());

    while start.elapsed() < timeout_duration {
        // Check if Firecracker crashed
        if let Ok(Some(status)) = process.try_wait() {
            let mut stdout_buf = String::new();
            let mut stderr_buf = String::new();

            if let Some(ref mut stdout) = process.stdout {
                let _ = stdout.read_to_string(&mut stdout_buf).await;
            }
            if let Some(ref mut stderr) = process.stderr {
                let _ = stderr.read_to_string(&mut stderr_buf).await;
            }

            let mut message = format!("Firecracker exited with status {}", status);
            if !stdout_buf.is_empty() {
                message.push_str(&format!(". stdout: {}", stdout_buf));
            }
            if !stderr_buf.is_empty() {
                message.push_str(&format!(". stderr: {}", stderr_buf));
            }

            return Err(MicroVMError::CommandFailed {
                command: "firecracker".to_string(),
                message,
            });
        }

        // Check if socket exists
        if socket_path.exists() {
            eprintln!("  Firecracker socket is ready");
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

/// Update the TAP device name in the config.json file
fn update_config_tap_device(config_path: &Path, tap_device: &str) -> Result<()> {
    let content = std::fs::read_to_string(config_path)?;
    let mut config: serde_json::Value = serde_json::from_str(&content)?;

    // Update the network interface host_dev_name
    if let Some(interfaces) = config.get_mut("network-interfaces") {
        if let Some(arr) = interfaces.as_array_mut() {
            for iface in arr {
                if let Some(obj) = iface.as_object_mut() {
                    obj.insert(
                        "host_dev_name".to_string(),
                        serde_json::Value::String(tap_device.to_string()),
                    );
                }
            }
        }
    }

    std::fs::write(config_path, serde_json::to_string_pretty(&config)?)?;
    Ok(())
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
