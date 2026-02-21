"""MicroVM runtime management."""

import json
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class MicroVM:
    """Represents a running MicroVM instance."""
    
    vm_id: str
    process: subprocess.Popen
    socket_path: Path
    tap_device: str
    ip_address: str
    port: int
    
    @property
    def url(self) -> str:
        """Get the HTTP URL for this VM."""
        return f"http://{self.ip_address}:{self.port}"
    
    def is_healthy(self, timeout: float = 1.0) -> bool:
        """Check if the VM is responding to health checks."""
        import urllib.request
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=timeout):
                return True
        except Exception:
            return False
    
    def stop(self) -> None:
        """Stop the MicroVM gracefully."""
        if self.process.poll() is None:
            # Send SIGTERM to Firecracker
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        
        # Cleanup TAP device
        subprocess.run(
            ["sudo", "ip", "link", "delete", self.tap_device],
            capture_output=True,
        )
        
        # Cleanup socket
        if self.socket_path.exists():
            self.socket_path.unlink()


def start_microvm(
    package_path: str,
    vm_id: Optional[str] = None,
    port: int = 8000,
    ip_address: str = "172.16.0.2",
    gateway: str = "172.16.0.1",
) -> MicroVM:
    """
    Start a MicroVM from a package.
    
    Args:
        package_path: Path to the .microvm package directory
        vm_id: Optional VM identifier (generated if not provided)
        port: Port to expose the environment on
        ip_address: IP address for the VM
        gateway: Gateway IP for the VM
        
    Returns:
        MicroVM instance representing the running VM
    """
    package = Path(package_path)
    
    if vm_id is None:
        vm_id = f"vm-{os.getpid()}-{int(time.time())}"
    
    # Setup networking
    tap_device = f"tap-{vm_id[:8]}"
    _setup_networking(tap_device, gateway)
    
    # Create API socket
    socket_path = Path(f"/tmp/firecracker-{vm_id}.socket")
    if socket_path.exists():
        socket_path.unlink()
    
    # Start Firecracker
    process = subprocess.Popen(
        [
            "firecracker",
            "--api-sock", str(socket_path),
            "--config-file", str(package / "config.json"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(package),
    )
    
    # Wait for socket
    _wait_for_socket(socket_path)
    
    # Configure and start the VM via API
    _configure_vm(socket_path, ip_address, gateway, tap_device)
    
    vm = MicroVM(
        vm_id=vm_id,
        process=process,
        socket_path=socket_path,
        tap_device=tap_device,
        ip_address=ip_address,
        port=port,
    )
    
    # Wait for VM to be healthy
    _wait_for_healthy(vm)
    
    return vm


def _setup_networking(tap_device: str, gateway: str) -> None:
    """Setup TAP device and networking for the VM."""
    # Create TAP device
    subprocess.run(
        ["sudo", "ip", "tuntap", "add", "dev", tap_device, "mode", "tap"],
        check=True,
    )
    
    # Configure TAP device
    subprocess.run(
        ["sudo", "ip", "addr", "add", f"{gateway}/24", "dev", tap_device],
        check=True,
    )
    
    subprocess.run(
        ["sudo", "ip", "link", "set", tap_device, "up"],
        check=True,
    )
    
    # Enable IP forwarding
    subprocess.run(
        ["sudo", "sysctl", "-w", "net.ipv4.ip_forward=1"],
        capture_output=True,
    )
    
    # Setup NAT
    subprocess.run(
        [
            "sudo", "iptables", "-t", "nat", "-A", "POSTROUTING",
            "-o", "eth0", "-j", "MASQUERADE"
        ],
        capture_output=True,
    )


def _wait_for_socket(socket_path: Path, timeout: float = 10.0) -> None:
    """Wait for the Firecracker API socket to be available."""
    start = time.time()
    while time.time() - start < timeout:
        if socket_path.exists():
            return
        time.sleep(0.1)
    raise TimeoutError(f"Firecracker socket not available after {timeout}s")


def _configure_vm(
    socket_path: Path,
    ip_address: str,
    gateway: str,
    tap_device: str,
) -> None:
    """Configure the VM via Firecracker API."""
    import urllib.request
    
    # The config.json already has most settings
    # Here we could add dynamic configuration if needed
    
    # Start the VM
    _api_request(socket_path, "PUT", "/actions", {"action_type": "InstanceStart"})


def _api_request(socket_path: Path, method: str, path: str, data: dict) -> dict:
    """Make an API request to Firecracker via Unix socket."""
    import http.client
    import json
    
    class UnixHTTPConnection(http.client.HTTPConnection):
        def __init__(self, socket_path: str):
            super().__init__("localhost")
            self.socket_path = socket_path
            
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(self.socket_path)
    
    conn = UnixHTTPConnection(str(socket_path))
    conn.request(method, path, json.dumps(data), {"Content-Type": "application/json"})
    response = conn.getresponse()
    
    if response.status >= 400:
        raise RuntimeError(f"API error: {response.status} {response.read().decode()}")
    
    body = response.read()
    return json.loads(body) if body else {}


def _wait_for_healthy(vm: MicroVM, timeout: float = 30.0) -> None:
    """Wait for the VM to pass health checks."""
    start = time.time()
    while time.time() - start < timeout:
        if vm.is_healthy():
            return
        time.sleep(0.5)
    raise TimeoutError(f"VM {vm.vm_id} not healthy after {timeout}s")
