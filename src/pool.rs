//! MicroVM pool management for high-throughput RL training

use crate::error::{MicroVMError, Result};
use crate::runtime::{start_microvm, MicroVM};
use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{Mutex, Notify};

/// Statistics for a MicroVM pool
#[derive(Debug, Clone, Default)]
pub struct PoolStats {
    pub total_vms: usize,
    pub available_vms: usize,
    pub in_use_vms: usize,
    pub total_acquisitions: u64,
    pub total_releases: u64,
    pub avg_acquire_time_ms: f64,
}

/// Internal pool state
struct PoolState {
    available: VecDeque<MicroVM>,
    in_use: Vec<String>, // VM IDs that are in use
    vm_counter: usize,
    stats: PoolStats,
    acquire_times: Vec<f64>,
    shutdown: bool,
}

/// Pool of pre-warmed MicroVMs for instant acquisition.
///
/// Maintains a pool of booted, idle MicroVMs that can be instantly
/// acquired for RL training episodes. When released, VMs are reset
/// and returned to the pool.
///
/// # Example
/// ```ignore
/// let pool = MicroVMPool::new("echo-env.microvm", 32, 2, "172.16", 8000).await?;
///
/// // Get a fresh VM (instant - already booted)
/// let vm = pool.acquire().await?;
///
/// // Use the environment...
///
/// // Return to pool (resets and recycles)
/// pool.release(vm).await?;
///
/// // Cleanup when done
/// pool.shutdown().await?;
/// ```
#[derive(Clone)]
pub struct MicroVMPool {
    package_path: PathBuf,
    size: usize,
    min_available: usize,
    ip_base: String,
    port: u16,
    state: Arc<Mutex<PoolState>>,
    available_notify: Arc<Notify>,
}

impl MicroVMPool {
    /// Initialize a MicroVM pool
    ///
    /// # Arguments
    /// * `package_path` - Path to the .microvm package
    /// * `size` - Total number of VMs in the pool
    /// * `min_available` - Minimum VMs to keep available (triggers pre-warming)
    /// * `ip_base` - Base IP prefix for VM addresses (e.g., "172.16")
    /// * `port` - Port for environment servers
    pub async fn new(
        package_path: &Path,
        size: usize,
        min_available: usize,
        ip_base: &str,
        port: u16,
    ) -> Result<Self> {
        let pool = Self {
            package_path: package_path.to_path_buf(),
            size,
            min_available,
            ip_base: ip_base.to_string(),
            port,
            state: Arc::new(Mutex::new(PoolState {
                available: VecDeque::new(),
                in_use: Vec::new(),
                vm_counter: 0,
                stats: PoolStats::default(),
                acquire_times: Vec::new(),
                shutdown: false,
            })),
            available_notify: Arc::new(Notify::new()),
        };

        // Initial warmup
        pool.warmup().await?;

        // Start background warmer
        let pool_clone = pool.clone();
        tokio::spawn(async move {
            pool_clone.warmer_loop().await;
        });

        Ok(pool)
    }

    /// Pre-warm the initial pool of VMs
    async fn warmup(&self) -> Result<()> {
        let mut handles = Vec::new();

        for _ in 0..self.size {
            let pool = self.clone();
            let handle = tokio::spawn(async move { pool.create_vm().await });
            handles.push(handle);
        }

        for handle in handles {
            let _ = handle.await;
        }

        Ok(())
    }

    /// Create a new MicroVM and add to available pool
    async fn create_vm(&self) -> Result<Option<MicroVM>> {
        let (vm_id, ip_address, gateway) = {
            let mut state = self.state.lock().await;
            if state.shutdown {
                return Ok(None);
            }

            state.vm_counter += 1;
            let counter = state.vm_counter;

            // Calculate IP address
            let subnet = (counter / 254) + 1;
            let host = (counter % 254) + 2;
            let ip_address = format!("{}.{}.{}", self.ip_base, subnet, host);
            let gateway = format!("{}.{}.1", self.ip_base, subnet);
            let vm_id = format!("pool-{}", counter);

            (vm_id, ip_address, gateway)
        };

        match start_microvm(
            &self.package_path,
            Some(&vm_id),
            self.port,
            &ip_address,
            &gateway,
        )
        .await
        {
            Ok(vm) => {
                let mut state = self.state.lock().await;
                state.available.push_back(vm);
                state.stats.total_vms += 1;
                state.stats.available_vms += 1;
                self.available_notify.notify_one();
                Ok(None) // VM is in the pool now
            }
            Err(e) => {
                eprintln!("Failed to create VM: {}", e);
                Ok(None)
            }
        }
    }

    /// Background task to maintain minimum available VMs
    async fn warmer_loop(&self) {
        loop {
            {
                let state = self.state.lock().await;
                if state.shutdown {
                    break;
                }

                let available = state.stats.available_vms;
                let total = state.stats.total_vms;

                if available < self.min_available && total < self.size {
                    drop(state);
                    let _ = self.create_vm().await;
                }
            }

            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    }

    /// Acquire a VM from the pool
    ///
    /// # Arguments
    /// * `timeout` - Maximum time to wait for a VM
    ///
    /// # Returns
    /// An available MicroVM instance
    pub async fn acquire(&self, timeout: Option<Duration>) -> Result<MicroVM> {
        let start = Instant::now();

        loop {
            // Try to get a VM
            {
                let mut state = self.state.lock().await;
                if let Some(vm) = state.available.pop_front() {
                    state.in_use.push(vm.vm_id.clone());
                    state.stats.available_vms -= 1;
                    state.stats.in_use_vms += 1;
                    state.stats.total_acquisitions += 1;

                    let acquire_time = start.elapsed().as_secs_f64() * 1000.0;
                    state.acquire_times.push(acquire_time);
                    if state.acquire_times.len() > 100 {
                        state.acquire_times.remove(0);
                    }
                    state.stats.avg_acquire_time_ms =
                        state.acquire_times.iter().sum::<f64>() / state.acquire_times.len() as f64;

                    return Ok(vm);
                }
            }

            // Check timeout
            if let Some(t) = timeout {
                if start.elapsed() >= t {
                    return Err(MicroVMError::PoolExhausted);
                }
            }

            // Wait for a VM to become available
            if let Some(t) = timeout {
                let remaining = t.saturating_sub(start.elapsed());
                tokio::select! {
                    _ = self.available_notify.notified() => {}
                    _ = tokio::time::sleep(remaining) => {
                        return Err(MicroVMError::PoolExhausted);
                    }
                }
            } else {
                self.available_notify.notified().await;
            }
        }
    }

    /// Release a VM back to the pool
    ///
    /// The VM will be reset before being made available again.
    pub async fn release(&self, vm: MicroVM) -> Result<()> {
        {
            let mut state = self.state.lock().await;
            if let Some(pos) = state.in_use.iter().position(|id| *id == vm.vm_id) {
                state.in_use.remove(pos);
                state.stats.in_use_vms -= 1;
            }
            state.stats.total_releases += 1;
        }

        // Reset the environment
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .map_err(|e| MicroVMError::Http(e))?;

        let reset_url = format!("{}/reset", vm.url());
        match client
            .post(&reset_url)
            .json(&serde_json::json!({}))
            .send()
            .await
        {
            Ok(_) => {
                // Return to available pool
                let mut state = self.state.lock().await;
                state.available.push_back(vm);
                state.stats.available_vms += 1;
                self.available_notify.notify_one();
            }
            Err(_) => {
                // If reset fails, destroy and create new VM
                let _ = vm.stop().await;
                let _ = self.create_vm().await;
            }
        }

        Ok(())
    }

    /// Get current pool statistics
    pub async fn stats(&self) -> PoolStats {
        let state = self.state.lock().await;
        state.stats.clone()
    }

    /// Shutdown the pool and stop all VMs
    pub async fn shutdown(&self) -> Result<()> {
        let vms_to_stop: Vec<MicroVM> = {
            let mut state = self.state.lock().await;
            state.shutdown = true;

            // Collect all available VMs
            state.available.drain(..).collect()
        };

        // Stop all VMs
        for vm in vms_to_stop {
            let _ = vm.stop().await;
        }

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pool_stats_default() {
        let stats = PoolStats::default();
        assert_eq!(stats.total_vms, 0);
        assert_eq!(stats.available_vms, 0);
        assert_eq!(stats.in_use_vms, 0);
        assert_eq!(stats.total_acquisitions, 0);
        assert_eq!(stats.total_releases, 0);
        assert_eq!(stats.avg_acquire_time_ms, 0.0);
    }

    #[test]
    fn test_ip_calculation() {
        // Test IP calculation logic
        let ip_base = "172.16";

        // First VM
        let counter = 1;
        let subnet = (counter / 254) + 1;
        let host = (counter % 254) + 2;
        assert_eq!(format!("{}.{}.{}", ip_base, subnet, host), "172.16.1.3");

        // 253rd VM (still in first subnet)
        let counter = 253;
        let subnet = (counter / 254) + 1;
        let host = (counter % 254) + 2;
        assert_eq!(format!("{}.{}.{}", ip_base, subnet, host), "172.16.1.255");

        // 254th VM (wraps to second subnet)
        let counter = 254;
        let subnet = (counter / 254) + 1;
        let host = (counter % 254) + 2;
        assert_eq!(format!("{}.{}.{}", ip_base, subnet, host), "172.16.2.2");
    }
}
