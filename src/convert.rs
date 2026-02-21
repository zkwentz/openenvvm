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

/// Base Alpine image for rootfs
const ALPINE_BASE: &str = "alpine:3.19";

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
/// # Arguments
/// * `env_path` - Path to OpenEnv environment directory or HuggingFace spec (hf:org/repo)
/// * `output_path` - Output path for the .microvm package
/// * `kernel_path` - Optional path to vmlinux kernel (downloads default if not provided)
/// * `memory_mb` - Memory allocation for the VM in MB
/// * `vcpu_count` - Number of virtual CPUs
///
/// # Returns
/// Path to the created .microvm package
pub fn convert_env_to_microvm(
    env_path: &str,
    output_path: &Path,
    kernel_path: Option<&Path>,
    memory_mb: u32,
    vcpu_count: u32,
) -> Result<PathBuf> {
    let output = output_path.to_path_buf();
    fs::create_dir_all(&output)?;

    let tmpdir = TempDir::new()?;
    let tmp = tmpdir.path();

    // Step 1: Resolve environment source
    let env_dir = resolve_env_source(env_path, tmp)?;

    // Step 2: Build rootfs
    let rootfs_path = tmp.join("rootfs.ext4");
    build_rootfs(&env_dir, &rootfs_path, 512)?;

    // Step 3: Handle kernel
    let kernel_dst = output.join("vmlinux");
    if let Some(kp) = kernel_path {
        fs::copy(kp, &kernel_dst)?;
    } else {
        download_kernel(&kernel_dst)?;
    }

    // Step 4: Copy rootfs to output
    fs::copy(&rootfs_path, output.join("rootfs.ext4"))?;

    // Step 5: Generate Firecracker config
    let config = generate_config("vmlinux", "rootfs.ext4", memory_mb, vcpu_count);
    let config_json = serde_json::to_string_pretty(&config)?;
    fs::write(output.join("config.json"), config_json)?;

    // Step 6: Create metadata
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

/// Resolve environment source (local path or HuggingFace)
fn resolve_env_source(env_path: &str, tmp: &Path) -> Result<PathBuf> {
    if let Some(repo_id) = env_path.strip_prefix("hf:") {
        // Download from HuggingFace
        let env_dir = tmp.join("env");
        let url = format!("https://huggingface.co/spaces/{}", repo_id);

        let output = Command::new("git")
            .args(["clone", &url, env_dir.to_str().unwrap()])
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

/// Build ext4 rootfs from OpenEnv environment
fn build_rootfs(env_dir: &Path, output_path: &Path, size_mb: u32) -> Result<()> {
    // Create empty ext4 image
    run_command(
        "dd",
        &[
            "if=/dev/zero",
            &format!("of={}", output_path.display()),
            "bs=1M",
            &format!("count={}", size_mb),
        ],
    )?;

    run_command("mkfs.ext4", &[output_path.to_str().unwrap()])?;

    // Mount and populate
    let mount_dir = TempDir::new()?;
    let mount_path = mount_dir.path();

    run_command(
        "sudo",
        &[
            "mount",
            "-o",
            "loop",
            output_path.to_str().unwrap(),
            mount_path.to_str().unwrap(),
        ],
    )?;

    // Use a closure to ensure we unmount even on error
    let result = (|| -> Result<()> {
        // Extract Alpine base
        let mount_str = mount_path.to_str().unwrap();
        run_command(
            "docker",
            &[
                "run",
                "--rm",
                "-v",
                &format!("{}:/mnt", mount_str),
                ALPINE_BASE,
                "sh",
                "-c",
                "cp -a / /mnt/ 2>/dev/null || true",
            ],
        )?;

        // Install Python and dependencies
        run_command(
            "sudo",
            &[
                "chroot",
                mount_str,
                "apk",
                "add",
                "--no-cache",
                "python3",
                "py3-pip",
                "py3-uvicorn",
            ],
        )?;

        // Copy environment code
        let env_dest = mount_path.join("app/env");
        fs::create_dir_all(&env_dest)?;
        copy_dir_recursive(env_dir, &env_dest)?;

        // Install environment dependencies
        let requirements = env_dir.join("server/requirements.txt");
        if requirements.exists() {
            run_command(
                "sudo",
                &[
                    "chroot",
                    mount_str,
                    "pip3",
                    "install",
                    "-r",
                    "/app/env/server/requirements.txt",
                ],
            )?;
        }

        // Create init script
        let init_script = mount_path.join("init.sh");
        fs::write(
            &init_script,
            "#!/bin/sh\ncd /app/env\nexec uvicorn server.app:app --host 0.0.0.0 --port 8000\n",
        )?;

        // Make init script executable
        run_command("sudo", &["chmod", "755", init_script.to_str().unwrap()])?;

        Ok(())
    })();

    // Always unmount
    let _ = run_command("sudo", &["umount", mount_path.to_str().unwrap()]);

    result
}

/// Download the default Firecracker kernel
fn download_kernel(output_path: &Path) -> Result<()> {
    run_command(
        "curl",
        &["-L", "-o", output_path.to_str().unwrap(), DEFAULT_KERNEL_URL],
    )
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

/// Run a command and return an error if it fails
fn run_command(cmd: &str, args: &[&str]) -> Result<()> {
    let output = Command::new(cmd).args(args).output()?;

    if !output.status.success() {
        return Err(MicroVMError::CommandFailed {
            command: format!("{} {}", cmd, args.join(" ")),
            message: String::from_utf8_lossy(&output.stderr).to_string(),
        });
    }

    Ok(())
}

/// Recursively copy a directory
fn copy_dir_recursive(src: &Path, dst: &Path) -> Result<()> {
    if !dst.exists() {
        fs::create_dir_all(dst)?;
    }

    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let src_path = entry.path();
        let dst_path = dst.join(entry.file_name());

        if src_path.is_dir() {
            copy_dir_recursive(&src_path, &dst_path)?;
        } else {
            fs::copy(&src_path, &dst_path)?;
        }
    }

    Ok(())
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
