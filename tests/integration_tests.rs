//! Integration tests for openenvvm

use assert_cmd::Command;
use predicates::prelude::*;
use std::fs;
use tempfile::TempDir;

#[test]
fn test_cli_help() {
    let mut cmd = Command::cargo_bin("openenvvm").unwrap();
    cmd.arg("--help")
        .assert()
        .success()
        .stdout(predicate::str::contains("Convert OpenEnv environments"));
}

#[test]
fn test_cli_version() {
    let mut cmd = Command::cargo_bin("openenvvm").unwrap();
    cmd.arg("--version")
        .assert()
        .success()
        .stdout(predicate::str::contains("openenvvm"));
}

#[test]
fn test_convert_help() {
    let mut cmd = Command::cargo_bin("openenvvm").unwrap();
    cmd.args(["convert", "--help"])
        .assert()
        .success()
        .stdout(predicate::str::contains("Convert an OpenEnv environment"));
}

#[test]
fn test_run_help() {
    let mut cmd = Command::cargo_bin("openenvvm").unwrap();
    cmd.args(["run", "--help"])
        .assert()
        .success()
        .stdout(predicate::str::contains("Run a microVM package"));
}

#[test]
fn test_pool_help() {
    let mut cmd = Command::cargo_bin("openenvvm").unwrap();
    cmd.args(["pool", "--help"])
        .assert()
        .success()
        .stdout(predicate::str::contains("Run a pool of microVMs"));
}

#[test]
fn test_convert_missing_output() {
    let mut cmd = Command::cargo_bin("openenvvm").unwrap();
    cmd.args(["convert", "some/path"])
        .assert()
        .failure()
        .stderr(predicate::str::contains("--output"));
}

#[test]
fn test_convert_invalid_path() {
    let tmp = TempDir::new().unwrap();
    let output = tmp.path().join("output.microvm");

    let mut cmd = Command::cargo_bin("openenvvm").unwrap();
    cmd.args([
        "convert",
        "/nonexistent/path/to/env",
        "-o",
        output.to_str().unwrap(),
    ])
    .assert()
    .failure();
}

#[test]
fn test_run_missing_package() {
    let mut cmd = Command::cargo_bin("openenvvm").unwrap();
    cmd.args(["run", "/nonexistent/package.microvm"])
        .assert()
        .failure();
}

/// Test that we can create a mock microvm package structure
#[test]
fn test_package_structure() {
    let tmp = TempDir::new().unwrap();
    let pkg = tmp.path().join("test.microvm");
    fs::create_dir_all(&pkg).unwrap();

    // Create minimal package files
    fs::write(pkg.join("vmlinux"), b"mock kernel").unwrap();
    fs::write(pkg.join("rootfs.ext4"), b"mock rootfs").unwrap();
    fs::write(
        pkg.join("config.json"),
        r#"{
            "boot-source": {
                "kernel_image_path": "vmlinux",
                "boot_args": "console=ttyS0"
            },
            "drives": [{
                "drive_id": "rootfs",
                "path_on_host": "rootfs.ext4",
                "is_root_device": true,
                "is_read_only": false
            }],
            "machine-config": {
                "vcpu_count": 1,
                "mem_size_mib": 256
            },
            "network-interfaces": [{
                "iface_id": "eth0",
                "guest_mac": "AA:FC:00:00:00:01",
                "host_dev_name": "tap0"
            }]
        }"#,
    )
    .unwrap();
    fs::write(
        pkg.join("metadata.json"),
        r#"{
            "version": "1.0",
            "env_name": "test",
            "memory_mb": 256,
            "vcpu_count": 1
        }"#,
    )
    .unwrap();

    // Verify all files exist
    assert!(pkg.join("vmlinux").exists());
    assert!(pkg.join("rootfs.ext4").exists());
    assert!(pkg.join("config.json").exists());
    assert!(pkg.join("metadata.json").exists());
}
