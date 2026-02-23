//! Convert OpenEnv environments to MicroVM packages

use crate::error::{MicroVMError, Result};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use tempfile::TempDir;

/// Default kernel URL for Firecracker
const DEFAULT_KERNEL_URL: &str =
    "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.6/x86_64/vmlinux-5.10.198";

/// Builder Docker image for the finalize phase (ext4 creation only)
const BUILDER_IMAGE: &str = "alpine:3.19";

/// Firecracker VM configuration
#[derive(Debug, Serialize, Deserialize)]
pub struct FirecrackerConfig {
    #[serde(rename = "boot-source")]
    pub boot_source: BootSource,
    pub drives: Vec<Drive>,
    #[serde(rename = "machine-config")]
    pub machine_config: MachineConfig,
    #[serde(rename = "network-interfaces")]
    pub network_interfaces: Vec<NetworkInterface>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct BootSource {
    pub kernel_image_path: String,
    pub boot_args: String,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Drive {
    pub drive_id: String,
    pub path_on_host: String,
    pub is_root_device: bool,
    pub is_read_only: bool,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct MachineConfig {
    pub vcpu_count: u32,
    pub mem_size_mib: u32,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct NetworkInterface {
    pub iface_id: String,
    pub guest_mac: String,
    pub host_dev_name: String,
}

/// Metadata for the microVM package
#[derive(Debug, Serialize, Deserialize)]
pub struct Metadata {
    pub version: String,
    pub env_name: String,
    pub memory_mb: u32,
    pub vcpu_count: u32,
}

/// Convert an OpenEnv environment to a MicroVM package.
///
/// This function uses Docker to perform the build, so it works on macOS, Linux, and Windows.
///
/// # Arguments
/// * `env_path` - Path to OpenEnv environment directory or HuggingFace spec (hf:org/repo)
/// * `output_path` - Output path for the .microvm package
/// * `kernel_path` - Optional path to vmlinux kernel (downloads default if not provided)
/// * `memory_mb` - Memory allocation for the VM in MB
/// * `vcpu_count` - Number of virtual CPUs
///
/// # Returns
/// Path to the created .microvm package
///
/// # Requirements
/// - Docker must be installed and running
pub fn convert_env_to_microvm(
    env_path: &str,
    output_path: &Path,
    kernel_path: Option<&Path>,
    memory_mb: u32,
    vcpu_count: u32,
) -> Result<PathBuf> {
    // Check Docker is available
    check_docker()?;

    let output = output_path.to_path_buf();
    fs::create_dir_all(&output)?;

    let tmpdir = TempDir::new()?;
    let tmp = tmpdir.path();

    // Step 1: Resolve environment source
    println!("  Resolving environment source...");
    let env_dir = resolve_env_source(env_path, tmp)?;
    let env_dir = fs::canonicalize(&env_dir)?;

    // Step 2: Build rootfs using Docker
    println!("  Building rootfs (this may take a moment)...");
    let rootfs_path = output.join("rootfs.ext4");
    build_rootfs_docker(&env_dir, &rootfs_path, 4096)?;

    // Step 3: Handle kernel
    println!("  Downloading kernel...");
    let kernel_dst = output.join("vmlinux");
    if let Some(kp) = kernel_path {
        fs::copy(kp, &kernel_dst)?;
    } else {
        download_kernel(&kernel_dst)?;
    }

    // Step 4: Generate Firecracker config
    println!("  Generating configuration...");
    let config = generate_config("vmlinux", "rootfs.ext4", memory_mb, vcpu_count);
    let config_json = serde_json::to_string_pretty(&config)?;
    fs::write(output.join("config.json"), config_json)?;

    // Step 5: Create metadata
    let env_name = env_dir
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("unknown")
        .to_string();

    let metadata = Metadata {
        version: "1.0".to_string(),
        env_name,
        memory_mb,
        vcpu_count,
    };
    let metadata_json = serde_json::to_string_pretty(&metadata)?;
    fs::write(output.join("metadata.json"), metadata_json)?;

    Ok(output)
}

/// Check if Docker is available
fn check_docker() -> Result<()> {
    let output = Command::new("docker").args(["version"]).output();

    match output {
        Ok(o) if o.status.success() => Ok(()),
        Ok(o) => Err(MicroVMError::CommandFailed {
            command: "docker version".to_string(),
            message: format!(
                "Docker is not running. Please start Docker Desktop.\n{}",
                String::from_utf8_lossy(&o.stderr)
            ),
        }),
        Err(_) => Err(MicroVMError::CommandFailed {
            command: "docker".to_string(),
            message:
                "Docker is not installed. Please install Docker Desktop from https://docker.com"
                    .to_string(),
        }),
    }
}

/// Resolve environment source (local path or HuggingFace)
fn resolve_env_source(env_path: &str, tmp: &Path) -> Result<PathBuf> {
    if let Some(repo_id) = env_path.strip_prefix("hf:") {
        // Download from HuggingFace
        let env_dir = tmp.join("env");
        let url = format!("https://huggingface.co/spaces/{}", repo_id);

        let output = Command::new("git")
            .args(["clone", "--depth", "1", &url, env_dir.to_str().unwrap()])
            .output()?;

        if !output.status.success() {
            return Err(MicroVMError::CommandFailed {
                command: "git clone".to_string(),
                message: String::from_utf8_lossy(&output.stderr).to_string(),
            });
        }

        Ok(env_dir)
    } else {
        let path = PathBuf::from(env_path);
        if !path.exists() {
            return Err(MicroVMError::InvalidEnvPath(env_path.to_string()));
        }
        Ok(path)
    }
}

/// Build ext4 rootfs using Docker (works on macOS, Linux, Windows)
///
/// This uses a two-phase approach for speed:
/// 1. Build a container with all dependencies installed
/// 2. Export as tarball and create ext4 image
fn build_rootfs_docker(env_dir: &Path, output_path: &Path, size_mb: u32) -> Result<()> {
    let output_dir = fs::canonicalize(output_path.parent().unwrap())?;
    let container_name = format!("openenv-build-{}", std::process::id());

    // Phase 1: Build container with all dependencies
    println!("    Creating build container...");

    // Get the env directory name for the COPY command
    let env_name = env_dir.file_name().unwrap().to_str().unwrap();

    // Create a Dockerfile for the build
    // Uses Debian slim for glibc compatibility with native Python packages
    let dockerfile = format!(
        r#"FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    git iproute2 build-essential python3-dev libffi-dev swig \
    cmake pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uvicorn fastapi

# Install openenv from the OpenEnv repository (provides openenv.core module)
RUN pip install --no-cache-dir git+https://github.com/meta-pytorch/OpenEnv.git || true

COPY {env_name} /app/env

# Install env deps from requirements.txt if available
RUN if [ -f /app/env/server/requirements.txt ]; then \
        pip install --no-cache-dir -r /app/env/server/requirements.txt || true; \
    fi

# Install the env package itself (without deps to avoid openenv-core resolution issues)
RUN if [ -f /app/env/pyproject.toml ]; then \
        pip install --no-cache-dir --no-deps /app/env || true; \
    fi

# Install pyproject.toml dependencies individually (skipping openenv-related packages
# which are already installed from git and cause resolution failures)
RUN if [ -f /app/env/pyproject.toml ]; then \
        python3 -c " \
import tomllib, sys; \
data = tomllib.load(open('/app/env/pyproject.toml', 'rb')); \
deps = data.get('project', {{}}).get('dependencies', []); \
[print(d) for d in deps if 'openenv' not in d.lower()] \
" 2>/dev/null | while read -r dep; do \
            echo \"Installing: $dep\"; \
            pip install --no-cache-dir \"$dep\" 2>&1 || echo \"WARN: Failed to install $dep\"; \
        done; \
    fi

RUN printf '#!/bin/sh\n\
echo "MicroVM init starting..."\n\
\n\
# Set up environment\n\
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n\
export HOME=/root\n\
export PYTHONPATH=/app:/app/env\n\
\n\
# Configure network\n\
echo "Configuring network..."\n\
ip addr add 172.16.0.2/24 dev eth0 2>&1 || echo "ip addr failed"\n\
ip link set eth0 up 2>&1 || echo "ip link failed"\n\
ip route add default via 172.16.0.1 dev eth0 2>&1 || echo "ip route failed"\n\
echo "Network configured"\n\
ip addr show eth0\n\
\n\
# Start the application\n\
echo "Starting uvicorn server..."\n\
cd /app\n\
exec uvicorn env.server.app:app --host 0.0.0.0 --port 8000 2>&1\n' > /init.sh && chmod 755 /init.sh
"#,
        env_name = env_name
    );

    let dockerfile_path = output_dir.join("Dockerfile.microvm");
    fs::write(&dockerfile_path, &dockerfile)?;

    // Build the container image - use the parent of env_dir as context
    let build_context = env_dir.parent().unwrap();
    let output = Command::new("docker")
        .args([
            "build",
            "-t",
            &container_name,
            "-f",
            dockerfile_path.to_str().unwrap(),
            build_context.to_str().unwrap(),
        ])
        .output()?;

    let _ = fs::remove_file(&dockerfile_path);

    if !output.status.success() {
        return Err(MicroVMError::CommandFailed {
            command: "docker build".to_string(),
            message: format!(
                "stdout: {}\nstderr: {}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            ),
        });
    }

    // Phase 2: Export container and create ext4 image
    println!("    Exporting filesystem...");

    // Create a container (don't run it)
    let output = Command::new("docker")
        .args([
            "create",
            "--name",
            &format!("{}-export", container_name),
            &container_name,
        ])
        .output()?;

    if !output.status.success() {
        let _ = Command::new("docker")
            .args(["rmi", "-f", &container_name])
            .output();
        return Err(MicroVMError::CommandFailed {
            command: "docker create".to_string(),
            message: String::from_utf8_lossy(&output.stderr).to_string(),
        });
    }

    // Export to tarball
    let tarball_path = output_dir.join("rootfs.tar");
    let output = Command::new("docker")
        .args([
            "export",
            "-o",
            tarball_path.to_str().unwrap(),
            &format!("{}-export", container_name),
        ])
        .output()?;

    // Cleanup container
    let _ = Command::new("docker")
        .args(["rm", "-f", &format!("{}-export", container_name)])
        .output();
    let _ = Command::new("docker")
        .args(["rmi", "-f", &container_name])
        .output();

    if !output.status.success() {
        return Err(MicroVMError::CommandFailed {
            command: "docker export".to_string(),
            message: String::from_utf8_lossy(&output.stderr).to_string(),
        });
    }

    // Phase 3: Create ext4 image from tarball (using Docker for Linux tools)
    println!("    Creating ext4 filesystem...");

    let build_script = format!(
        r#"#!/bin/sh
set -e
apk add --no-cache e2fsprogs tar

# Create ext4 image
dd if=/dev/zero of=/output/rootfs.ext4 bs=1M count={size}
mkfs.ext4 -F /output/rootfs.ext4

# Mount and extract
mkdir -p /mnt/rootfs
mount -o loop /output/rootfs.ext4 /mnt/rootfs
tar -xf /output/rootfs.tar -C /mnt/rootfs
umount /mnt/rootfs

# Clean up tarball
rm /output/rootfs.tar

echo "Rootfs complete!"
"#,
        size = size_mb
    );

    let script_path = output_dir.join("finalize.sh");
    fs::write(&script_path, &build_script)?;

    let output = Command::new("docker")
        .args([
            "run",
            "--rm",
            "--privileged",
            "-v",
            &format!("{}:/output", output_dir.display()),
            BUILDER_IMAGE,
            "/bin/sh",
            "/output/finalize.sh",
        ])
        .output()?;

    let _ = fs::remove_file(&script_path);
    let _ = fs::remove_file(&tarball_path); // In case it wasn't deleted

    if !output.status.success() {
        return Err(MicroVMError::CommandFailed {
            command: "docker run (finalize)".to_string(),
            message: format!(
                "stdout: {}\nstderr: {}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            ),
        });
    }

    // Verify the rootfs was created
    if !output_path.exists() {
        return Err(MicroVMError::CommandFailed {
            command: "build rootfs".to_string(),
            message: "Rootfs file was not created".to_string(),
        });
    }

    Ok(())
}

/// Download the default Firecracker kernel
fn download_kernel(output_path: &Path) -> Result<()> {
    let output = Command::new("curl")
        .args([
            "-L",
            "-f",
            "--progress-bar",
            "-o",
            output_path.to_str().unwrap(),
            DEFAULT_KERNEL_URL,
        ])
        .output()?;

    if !output.status.success() {
        return Err(MicroVMError::CommandFailed {
            command: "curl (download kernel)".to_string(),
            message: String::from_utf8_lossy(&output.stderr).to_string(),
        });
    }

    Ok(())
}

/// Generate Firecracker VM configuration
fn generate_config(
    kernel_path: &str,
    rootfs_path: &str,
    memory_mb: u32,
    vcpu_count: u32,
) -> FirecrackerConfig {
    FirecrackerConfig {
        boot_source: BootSource {
            kernel_image_path: kernel_path.to_string(),
            boot_args: "console=ttyS0 reboot=k panic=1 pci=off init=/init.sh".to_string(),
        },
        drives: vec![Drive {
            drive_id: "rootfs".to_string(),
            path_on_host: rootfs_path.to_string(),
            is_root_device: true,
            is_read_only: false,
        }],
        machine_config: MachineConfig {
            vcpu_count,
            mem_size_mib: memory_mb,
        },
        network_interfaces: vec![NetworkInterface {
            iface_id: "eth0".to_string(),
            guest_mac: "AA:FC:00:00:00:01".to_string(),
            host_dev_name: "tap0".to_string(),
        }],
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_generate_config() {
        let config = generate_config("vmlinux", "rootfs.ext4", 256, 2);

        assert_eq!(config.boot_source.kernel_image_path, "vmlinux");
        assert_eq!(config.machine_config.vcpu_count, 2);
        assert_eq!(config.machine_config.mem_size_mib, 256);
        assert_eq!(config.drives.len(), 1);
        assert!(config.drives[0].is_root_device);
        assert_eq!(config.network_interfaces.len(), 1);
    }

    #[test]
    fn test_config_serialization() {
        let config = generate_config("vmlinux", "rootfs.ext4", 512, 4);
        let json = serde_json::to_string(&config).unwrap();

        assert!(json.contains("boot-source"));
        assert!(json.contains("machine-config"));
        assert!(json.contains("network-interfaces"));
    }

    #[test]
    fn test_metadata_serialization() {
        let metadata = Metadata {
            version: "1.0".to_string(),
            env_name: "test-env".to_string(),
            memory_mb: 256,
            vcpu_count: 1,
        };

        let json = serde_json::to_string(&metadata).unwrap();
        let parsed: Metadata = serde_json::from_str(&json).unwrap();

        assert_eq!(parsed.version, "1.0");
        assert_eq!(parsed.env_name, "test-env");
    }

    #[test]
    fn test_resolve_local_path() {
        let tmp = TempDir::new().unwrap();
        let env_dir = tmp.path().join("test_env");
        fs::create_dir_all(&env_dir).unwrap();

        let result = resolve_env_source(env_dir.to_str().unwrap(), tmp.path());
        assert!(result.is_ok());
        assert_eq!(result.unwrap(), env_dir);
    }

    #[test]
    fn test_resolve_nonexistent_path() {
        let tmp = TempDir::new().unwrap();
        let result = resolve_env_source("/nonexistent/path", tmp.path());
        assert!(result.is_err());
    }
}
