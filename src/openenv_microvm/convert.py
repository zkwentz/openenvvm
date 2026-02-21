"""Convert OpenEnv environments to MicroVM packages."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


# Minimal kernel for Firecracker (can be downloaded or built)
DEFAULT_KERNEL_URL = "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.6/x86_64/vmlinux-5.10.198"

# Base Alpine rootfs with Python
ALPINE_BASE = "alpine:3.19"


def convert_env_to_microvm(
    env_path: str,
    output_path: str,
    kernel_path: Optional[str] = None,
    memory_mb: int = 256,
    vcpu_count: int = 1,
) -> Path:
    """
    Convert an OpenEnv environment to a MicroVM package.
    
    Args:
        env_path: Path to OpenEnv environment directory or HuggingFace spec (hf:org/repo)
        output_path: Output path for the .microvm package
        kernel_path: Optional path to vmlinux kernel (downloads default if not provided)
        memory_mb: Memory allocation for the VM in MB
        vcpu_count: Number of virtual CPUs
        
    Returns:
        Path to the created .microvm package
    """
    output = Path(output_path)
    output.mkdir(parents=True, exist_ok=True)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        
        # Step 1: Resolve environment source
        env_dir = _resolve_env_source(env_path, tmp)
        
        # Step 2: Build rootfs from Dockerfile
        rootfs_path = tmp / "rootfs.ext4"
        _build_rootfs(env_dir, rootfs_path)
        
        # Step 3: Download or copy kernel
        kernel_dst = output / "vmlinux"
        if kernel_path:
            shutil.copy(kernel_path, kernel_dst)
        else:
            _download_kernel(kernel_dst)
        
        # Step 4: Copy rootfs to output
        shutil.copy(rootfs_path, output / "rootfs.ext4")
        
        # Step 5: Generate Firecracker config
        config = _generate_config(
            kernel_path="vmlinux",
            rootfs_path="rootfs.ext4",
            memory_mb=memory_mb,
            vcpu_count=vcpu_count,
        )
        
        with open(output / "config.json", "w") as f:
            json.dump(config, f, indent=2)
        
        # Step 6: Create metadata
        metadata = {
            "version": "1.0",
            "env_name": env_dir.name,
            "memory_mb": memory_mb,
            "vcpu_count": vcpu_count,
        }
        
        with open(output / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)
    
    return output


def _resolve_env_source(env_path: str, tmp: Path) -> Path:
    """Resolve environment source (local path or HuggingFace)."""
    if env_path.startswith("hf:"):
        # Download from HuggingFace
        repo_id = env_path[3:]
        env_dir = tmp / "env"
        subprocess.run(
            ["git", "clone", f"https://huggingface.co/spaces/{repo_id}", str(env_dir)],
            check=True,
            capture_output=True,
        )
        return env_dir
    else:
        return Path(env_path)


def _build_rootfs(env_dir: Path, output_path: Path, size_mb: int = 512) -> None:
    """Build ext4 rootfs from OpenEnv environment."""
    # Create empty ext4 image
    subprocess.run(
        ["dd", "if=/dev/zero", f"of={output_path}", "bs=1M", f"count={size_mb}"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["mkfs.ext4", str(output_path)],
        check=True,
        capture_output=True,
    )
    
    # Mount and populate
    with tempfile.TemporaryDirectory() as mount_dir:
        subprocess.run(
            ["sudo", "mount", "-o", "loop", str(output_path), mount_dir],
            check=True,
        )
        
        try:
            # Extract Alpine base
            subprocess.run(
                [
                    "docker", "run", "--rm",
                    "-v", f"{mount_dir}:/mnt",
                    ALPINE_BASE,
                    "sh", "-c", "cp -a / /mnt/ 2>/dev/null || true"
                ],
                check=True,
            )
            
            # Install Python and dependencies
            subprocess.run(
                [
                    "sudo", "chroot", mount_dir,
                    "apk", "add", "--no-cache",
                    "python3", "py3-pip", "py3-uvicorn",
                ],
                check=True,
            )
            
            # Copy environment code
            env_dest = Path(mount_dir) / "app" / "env"
            env_dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(env_dir, env_dest, dirs_exist_ok=True)
            
            # Install environment dependencies
            requirements = env_dir / "server" / "requirements.txt"
            if requirements.exists():
                subprocess.run(
                    [
                        "sudo", "chroot", mount_dir,
                        "pip3", "install", "-r", "/app/env/server/requirements.txt"
                    ],
                    check=True,
                )
            
            # Create init script
            init_script = Path(mount_dir) / "init.sh"
            init_script.write_text(
                "#!/bin/sh\n"
                "cd /app/env\n"
                "exec uvicorn server.app:app --host 0.0.0.0 --port 8000\n"
            )
            init_script.chmod(0o755)
            
        finally:
            subprocess.run(["sudo", "umount", mount_dir], check=True)


def _download_kernel(output_path: Path) -> None:
    """Download the default Firecracker kernel."""
    subprocess.run(
        ["curl", "-L", "-o", str(output_path), DEFAULT_KERNEL_URL],
        check=True,
    )


def _generate_config(
    kernel_path: str,
    rootfs_path: str,
    memory_mb: int,
    vcpu_count: int,
) -> dict:
    """Generate Firecracker VM configuration."""
    return {
        "boot-source": {
            "kernel_image_path": kernel_path,
            "boot_args": "console=ttyS0 reboot=k panic=1 pci=off init=/init.sh",
        },
        "drives": [
            {
                "drive_id": "rootfs",
                "path_on_host": rootfs_path,
                "is_root_device": True,
                "is_read_only": False,
            }
        ],
        "machine-config": {
            "vcpu_count": vcpu_count,
            "mem_size_mib": memory_mb,
        },
        "network-interfaces": [
            {
                "iface_id": "eth0",
                "guest_mac": "AA:FC:00:00:00:01",
                "host_dev_name": "tap0",
            }
        ],
    }
