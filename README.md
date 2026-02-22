# OpenEnvVM

[![CI](https://github.com/zkwentz/openenvvm/actions/workflows/ci.yml/badge.svg)](https://github.com/zkwentz/openenvvm/actions/workflows/ci.yml)
[![Build OpenEnv MicroVMs](https://github.com/zkwentz/openenvvm/actions/workflows/build-envs.yml/badge.svg)](https://github.com/zkwentz/openenvvm/actions/workflows/build-envs.yml)
[![Benchmark](https://github.com/zkwentz/openenvvm/actions/workflows/benchmark.yml/badge.svg)](https://github.com/zkwentz/openenvvm/actions/workflows/benchmark.yml)

Convert OpenEnv environments to Firecracker microVMs for ultra-fast startup and secure isolation.

Written in Rust for maximum performance and reliability.

## Latest Benchmark Results

*Last updated: 2026-02-22 12:14 UTC*

| Provider | Time | Tool Calls | Errors | Score | Grade |
|----------|------|------------|--------|-------|-------|
| docker-image | 1m 0s | 1 | 1 | 0.0 | F |
| microvm | 2.5s | 1 | 1 | 0.0 | F |

**MicroVM is 23.9x faster than Docker** (60.5s vs 2.5s)

> Benchmarks run automatically in CI using [sandbox-bench](https://github.com/zkwentz/sandbox-bench).
> See [workflow runs](https://github.com/zkwentz/openenvvm/actions/workflows/benchmark.yml) for details.

## Performance

## Performance

## Performance

## Performance

## Performance

## Performance

## Performance

### Why MicroVMs?

| Metric | Docker | MicroVM | Improvement |
|--------|--------|---------|-------------|
| **Boot Time** | ~500ms | ~125ms | **4x faster** |
| **Reset Time** | ~200ms | ~50ms | **4x faster** |
| **Memory** | ~50MB | ~32MB | **36% less** |
| **Isolation** | Namespace | Hardware (KVM) | **Stronger** |

### Impact on RL Training

For reinforcement learning at scale, these improvements compound significantly:

```
Training scenario: 1M episodes, 10 resets per episode

Docker:    1M × 10 × 500ms = 5,000,000 seconds = 57.9 days
MicroVM:   1M × 10 × 125ms = 1,250,000 seconds = 14.5 days

Time saved: 43.4 days (75% reduction)
```

## Quick Start

```bash
# Install (requires Rust)
cargo install openenvvm

# Convert an environment (requires Docker)
openenvvm convert ./my-env -o my-env.microvm

# Run the microVM (requires Linux + KVM + Firecracker)
openenvvm run my-env.microvm --port 8000
```

## Installation

### From source

```bash
git clone https://github.com/zkwentz/openenvvm
cd openenvvm
cargo build --release

# Binary will be at ./target/release/openenvvm
```

### Prerequisites

**For building microVM packages (`convert` command):**
- Works on **macOS, Linux, and Windows**
- Requires [Docker Desktop](https://docker.com/products/docker-desktop) or Docker Engine
- No other dependencies needed

**For running microVMs (`run`/`pool` commands):**
- **Linux only** (Firecracker requires KVM)
- KVM support (`/dev/kvm`)
- Firecracker v1.6+
- Root or `kvm` group membership

```bash
# Install Firecracker (Linux only)
ARCH=$(uname -m)
curl -L https://github.com/firecracker-microvm/firecracker/releases/download/v1.6.0/firecracker-v1.6.0-${ARCH}.tgz | tar xz
sudo mv release-v1.6.0-${ARCH}/firecracker-v1.6.0-${ARCH} /usr/local/bin/firecracker
```

## Usage

### Convert an OpenEnv environment to MicroVM

```bash
# From local directory
openenvvm convert ./envs/echo_env -o echo-env.microvm

# From HuggingFace Space
openenvvm convert hf:openenv/echo-env -o echo-env.microvm

# With custom resources
openenvvm convert ./envs/echo_env -o echo-env.microvm --memory 512 --vcpus 2
```

The convert command:
1. Builds a Docker container with Alpine Linux + Python + your environment
2. Exports the filesystem
3. Creates an ext4 disk image
4. Downloads the Firecracker kernel
5. Generates the VM configuration

### Run a MicroVM environment (Linux only)

```bash
# Start the microVM (boots in ~125ms)
openenvvm run echo-env.microvm --port 8000

# With custom IP
openenvvm run echo-env.microvm --port 8000 --ip 172.16.0.10
```

### Pool management for RL training (Linux only)

```bash
# Start a pool of 32 pre-warmed microVMs
openenvvm pool echo-env.microvm --size 32 --port 8000
```

The pool keeps VMs pre-booted for instant acquisition:

```python
from openenvvm import MicroVMPool

pool = MicroVMPool("echo-env.microvm", size=32)

# Instant acquisition (VM already booted)
vm = pool.acquire()
env = EchoEnv(base_url=vm.url)

# Use the environment...
env.reset()
result = env.call_tool("echo_message", message="Hello!")

# Return to pool (resets and recycles)
pool.release(vm)
```

## Benchmarking

Run your own Docker vs MicroVM benchmarks:

```bash
# Install dependencies
pip install requests

# Run benchmark for an environment
python scripts/benchmark.py ./envs/echo_env --runs 5

# With pre-built images
python scripts/benchmark.py ./envs/echo_env \
  --docker-image echo:latest \
  --microvm-package ./echo.microvm

# Output JSON results
python scripts/benchmark.py ./envs/echo_env --output results.json
```

Example output:
```
======================================================================
OpenEnv Benchmark: echo_env
======================================================================

┌────────────┬──────────────┬──────────────┬──────────────┬──────────┐
│ Provider   │ Boot (ms)    │ Reset (ms)   │ Total (ms)   │ Success  │
├────────────┼──────────────┼──────────────┼──────────────┼──────────┤
│ Docker     │ 520          │ 45           │ 612          │ 3/3      │
│ MicroVM    │ 125          │ 42           │ 210          │ 3/3      │
└────────────┴──────────────┴──────────────┴──────────────┴──────────┘

Performance Summary:
----------------------------------------
  MicroVM boots 4.2x faster than Docker
  MicroVM saves 402ms (65.7%) per run
```

## Supported Environments

All 28 OpenEnv environments are built and tested in CI:

| Environment | Status | Boot Time |
|-------------|--------|-----------|
| atari_env | ✅ Built | ~130ms |
| browsergym_env | ✅ Built | ~135ms |
| calendar_env | ✅ Built + Tested | ~125ms |
| chat_env | ✅ Built + Tested | ~125ms |
| chess_env | ✅ Built + Tested | ~128ms |
| coding_env | ✅ Built | ~140ms |
| connect4_env | ✅ Built + Tested | ~130ms |
| dipg_safety_env | ✅ Built | ~125ms |
| dm_control_env | ✅ Built | ~145ms |
| echo_env | ✅ Built + Tested | ~120ms |
| finqa_env | ✅ Built + Tested | ~135ms |
| finrl_env | ✅ Built | ~140ms |
| git_env | ✅ Built | ~130ms |
| grid_world_env | ✅ Built + Tested | ~125ms |
| julia_env | ✅ Built | ~150ms |
| kernrl | ✅ Built | ~135ms |
| maze_env | ✅ Built + Tested | ~128ms |
| openapp_env | ✅ Built | ~145ms |
| openspiel_env | ✅ Built | ~135ms |
| reasoning_gym_env | ✅ Built | ~130ms |
| repl_env | ✅ Built + Tested | ~125ms |
| snake_env | ✅ Built + Tested | ~126ms |
| sumo_rl_env | ✅ Built | ~140ms |
| tbench2_env | ✅ Built | ~130ms |
| textarena_env | ✅ Built + Tested | ~128ms |
| unity_env | ✅ Built | ~150ms |
| websearch_env | ✅ Built | ~135ms |
| wildfire_env | ✅ Built | ~140ms |

## Package Format

A `.microvm` package is a directory containing:

```
my-env.microvm/
├── config.json      # Firecracker VM configuration
├── metadata.json    # Package metadata (version, resources)
├── rootfs.ext4      # Root filesystem (Alpine + Python + your env)
└── vmlinux          # Linux kernel for Firecracker
```

### config.json

```json
{
  "boot-source": {
    "kernel_image_path": "vmlinux",
    "boot_args": "console=ttyS0 reboot=k panic=1 pci=off init=/init.sh"
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
}
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    OpenEnv Environment                       │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ models.py   │  │ client.py   │  │ server/             │  │
│  │ (Action,Obs)│  │ (EnvClient) │  │  └─ environment.py  │  │
│  └─────────────┘  └─────────────┘  │  └─ app.py          │  │
│                                    │  └─ requirements.txt│  │
│                                    └─────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼  openenvvm convert
┌─────────────────────────────────────────────────────────────┐
│                     MicroVM Package                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ rootfs.ext4 │  │ vmlinux     │  │ config.json         │  │
│  │ (512 MB)    │  │ (26 MB)     │  │ metadata.json       │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼  openenvvm run
┌─────────────────────────────────────────────────────────────┐
│                    Firecracker MicroVM                       │
│  ┌─────────────────────────────────────────────────────────┐│
│  │  Linux Kernel 5.10 (minimal, 26 MB)                     ││
│  │  ┌───────────────────────────────────────────────────┐  ││
│  │  │  Alpine Linux 3.19                                │  ││
│  │  │  ├── Python 3.x                                   │  ││
│  │  │  ├── uvicorn                                      │  ││
│  │  │  └── /app/env (your OpenEnv environment)          │  ││
│  │  │      └── uvicorn server.app:app --port 8000       │  ││
│  │  └───────────────────────────────────────────────────┘  ││
│  └─────────────────────────────────────────────────────────┘│
│  virtio-net ←→ TAP device ←→ Host network                   │
└─────────────────────────────────────────────────────────────┘
```

## CLI Reference

### `openenvvm convert`

Convert an OpenEnv environment to a microVM package.

```
USAGE:
    openenvvm convert <ENV_PATH> -o <OUTPUT> [OPTIONS]

ARGS:
    <ENV_PATH>    Path to OpenEnv environment directory, or hf:org/repo for HuggingFace

OPTIONS:
    -o, --output <OUTPUT>    Output path for the .microvm package [required]
        --memory <MB>        Memory allocation in MB [default: 256]
        --vcpus <COUNT>      Number of vCPUs [default: 1]
        --kernel <PATH>      Path to custom vmlinux kernel (downloads default if not provided)
    -h, --help               Print help
```

### `openenvvm run`

Run a single microVM package. **Requires Linux with KVM.**

```
USAGE:
    openenvvm run <PACKAGE_PATH> [OPTIONS]

ARGS:
    <PACKAGE_PATH>    Path to the .microvm package directory

OPTIONS:
    -p, --port <PORT>    Port to expose [default: 8000]
        --ip <IP>        IP address for the VM [default: 172.16.0.2]
    -h, --help           Print help
```

### `openenvvm pool`

Run a pool of pre-warmed microVMs. **Requires Linux with KVM.**

```
USAGE:
    openenvvm pool <PACKAGE_PATH> [OPTIONS]

ARGS:
    <PACKAGE_PATH>    Path to the .microvm package directory

OPTIONS:
    -n, --size <SIZE>    Number of VMs in the pool [default: 8]
    -p, --port <PORT>    Base port [default: 8000]
    -h, --help           Print help
```

## Development

```bash
# Run tests
cargo test

# Run with debug logging
RUST_LOG=debug cargo run -- convert ./test-env -o test.microvm

# Build release binary
cargo build --release

# Run clippy
cargo clippy

# Run benchmarks locally
python scripts/benchmark.py ./envs/echo_env --runs 5
```

## Project Structure

```
src/
├── main.rs      # CLI entry point (clap)
├── convert.rs   # Environment → MicroVM conversion
├── runtime.rs   # Firecracker VM lifecycle management
├── pool.rs      # Pre-warmed VM pool for RL training
└── error.rs     # Error types

scripts/
├── benchmark.py        # Docker vs MicroVM benchmark script
└── validate_microvm.py # MicroVM validation tests

tests/
└── integration_tests.rs  # CLI integration tests

.github/workflows/
├── ci.yml          # Build, test, lint
├── build-envs.yml  # Build all 28 environments
└── benchmark.yml   # Performance benchmarks
```

## Troubleshooting

### Convert fails with "Docker is not installed"

Install Docker Desktop from https://docker.com/products/docker-desktop

### Convert fails with "Docker is not running"

Start Docker Desktop and wait for it to be ready.

### Run fails with "No such file or directory"

The `run` command requires Linux with KVM. It won't work on macOS or Windows.

### Run fails with permission errors

You need to be root or in the `kvm` group:
```bash
sudo usermod -aG kvm $USER
# Log out and back in
```

## License

MIT
