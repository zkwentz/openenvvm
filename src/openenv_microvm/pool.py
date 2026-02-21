"""MicroVM pool management for high-throughput RL training."""

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .runtime import MicroVM, start_microvm


@dataclass
class PoolStats:
    """Statistics for a MicroVM pool."""
    
    total_vms: int = 0
    available_vms: int = 0
    in_use_vms: int = 0
    total_acquisitions: int = 0
    total_releases: int = 0
    avg_acquire_time_ms: float = 0.0


class MicroVMPool:
    """
    Pool of pre-warmed MicroVMs for instant acquisition.
    
    Maintains a pool of booted, idle MicroVMs that can be instantly
    acquired for RL training episodes. When released, VMs are reset
    and returned to the pool.
    
    Example:
        >>> pool = MicroVMPool("echo-env.microvm", size=32)
        >>> 
        >>> # Acquire a VM (instant - already booted)
        >>> vm = pool.acquire()
        >>> env = EchoEnv(base_url=vm.url)
        >>> 
        >>> # Use the environment
        >>> env.reset()
        >>> result = env.call_tool("echo_message", message="Hello!")
        >>> 
        >>> # Return to pool
        >>> pool.release(vm)
        >>> 
        >>> # Cleanup when done
        >>> pool.shutdown()
    """
    
    def __init__(
        self,
        package_path: str,
        size: int = 8,
        min_available: int = 2,
        ip_base: str = "172.16",
        port: int = 8000,
    ):
        """
        Initialize a MicroVM pool.
        
        Args:
            package_path: Path to the .microvm package
            size: Total number of VMs in the pool
            min_available: Minimum VMs to keep available (triggers pre-warming)
            ip_base: Base IP prefix for VM addresses (e.g., "172.16")
            port: Port for environment servers
        """
        self.package_path = Path(package_path)
        self.size = size
        self.min_available = min_available
        self.ip_base = ip_base
        self.port = port
        
        self._available: queue.Queue[MicroVM] = queue.Queue()
        self._in_use: List[MicroVM] = []
        self._lock = threading.Lock()
        self._shutdown = False
        self._vm_counter = 0
        
        # Stats tracking
        self._stats = PoolStats()
        self._acquire_times: List[float] = []
        
        # Start background warmer thread
        self._warmer_thread = threading.Thread(target=self._warmer_loop, daemon=True)
        self._warmer_thread.start()
        
        # Initial pool warmup
        self._warmup()
    
    def _warmup(self) -> None:
        """Pre-warm the initial pool of VMs."""
        threads = []
        for _ in range(self.size):
            t = threading.Thread(target=self._create_vm)
            t.start()
            threads.append(t)
        
        for t in threads:
            t.join()
    
    def _create_vm(self) -> Optional[MicroVM]:
        """Create a new MicroVM and add to available pool."""
        if self._shutdown:
            return None
        
        with self._lock:
            self._vm_counter += 1
            vm_id = f"pool-{self._vm_counter}"
            
            # Calculate IP address
            subnet = (self._vm_counter // 254) + 1
            host = (self._vm_counter % 254) + 2
            ip_address = f"{self.ip_base}.{subnet}.{host}"
            gateway = f"{self.ip_base}.{subnet}.1"
        
        try:
            vm = start_microvm(
                str(self.package_path),
                vm_id=vm_id,
                port=self.port,
                ip_address=ip_address,
                gateway=gateway,
            )
            
            self._available.put(vm)
            
            with self._lock:
                self._stats.total_vms += 1
                self._stats.available_vms += 1
            
            return vm
            
        except Exception as e:
            print(f"Failed to create VM: {e}")
            return None
    
    def _warmer_loop(self) -> None:
        """Background thread to maintain minimum available VMs."""
        while not self._shutdown:
            with self._lock:
                available = self._stats.available_vms
                total = self._stats.total_vms
            
            # Create more VMs if below minimum
            if available < self.min_available and total < self.size:
                self._create_vm()
            
            time.sleep(0.5)
    
    def acquire(self, timeout: Optional[float] = None) -> MicroVM:
        """
        Acquire a VM from the pool.
        
        Args:
            timeout: Maximum time to wait for a VM (None = wait forever)
            
        Returns:
            An available MicroVM instance
            
        Raises:
            queue.Empty: If timeout expires with no VM available
        """
        start = time.time()
        
        vm = self._available.get(timeout=timeout)
        
        acquire_time = (time.time() - start) * 1000
        
        with self._lock:
            self._in_use.append(vm)
            self._stats.available_vms -= 1
            self._stats.in_use_vms += 1
            self._stats.total_acquisitions += 1
            
            self._acquire_times.append(acquire_time)
            if len(self._acquire_times) > 100:
                self._acquire_times = self._acquire_times[-100:]
            self._stats.avg_acquire_time_ms = sum(self._acquire_times) / len(self._acquire_times)
        
        return vm
    
    def release(self, vm: MicroVM) -> None:
        """
        Release a VM back to the pool.
        
        The VM will be reset before being made available again.
        
        Args:
            vm: The MicroVM to release
        """
        with self._lock:
            if vm in self._in_use:
                self._in_use.remove(vm)
                self._stats.in_use_vms -= 1
            self._stats.total_releases += 1
        
        # Reset the environment
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{vm.url}/reset",
                method="POST",
                headers={"Content-Type": "application/json"},
                data=b"{}",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            # If reset fails, destroy and create new VM
            vm.stop()
            self._create_vm()
            return
        
        # Return to available pool
        self._available.put(vm)
        
        with self._lock:
            self._stats.available_vms += 1
    
    def stats(self) -> PoolStats:
        """Get current pool statistics."""
        with self._lock:
            return PoolStats(
                total_vms=self._stats.total_vms,
                available_vms=self._stats.available_vms,
                in_use_vms=self._stats.in_use_vms,
                total_acquisitions=self._stats.total_acquisitions,
                total_releases=self._stats.total_releases,
                avg_acquire_time_ms=self._stats.avg_acquire_time_ms,
            )
    
    def shutdown(self) -> None:
        """Shutdown the pool and stop all VMs."""
        self._shutdown = True
        
        # Stop all in-use VMs
        with self._lock:
            for vm in self._in_use:
                vm.stop()
            self._in_use.clear()
        
        # Stop all available VMs
        while not self._available.empty():
            try:
                vm = self._available.get_nowait()
                vm.stop()
            except queue.Empty:
                break
        
        self._warmer_thread.join(timeout=2)
    
    def __enter__(self) -> "MicroVMPool":
        return self
    
    def __exit__(self, *args) -> None:
        self.shutdown()
