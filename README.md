# OpenEnv → MicroVM Converter

Convert OpenEnv environments to Firecracker microVMs for ultra-fast startup and secure isolation.

Written in Rust for maximum performance and reliability.

## Why MicroVMs?

| Feature | Docker | MicroVM |
|---------|--------|---------|
| Startup | ~500ms | ~125ms |
| Isolation | Namespace | Hardware (KVM) |
| Memory | Shared kernel | Dedicated |
| Security | Container escape risk | VM-level isolation |

For RL training at scale, microVMs provide:
- **Faster reset()** — critical for high-throughput training loops
- **Better isolation** — each episode in true hardware isolation
- **Deterministic execution** — no noisy neighbor effects

## Quick Start

```bash
# Install (requires Rust)
cargo install openenv-microvm

# Convert an environment (requires Docker)
openenv-microvm convert ./my-env -o my-env.microvm

# Run the microVM (requires Linux + KVM + Firecracker)
openenv-microvm run my-env.microvm --port 8000
```

## Installation

### From source

```bash
git clone https://github.com/zkwentz/openenv-microvm
cd openenv-microvm
cargo build --release

# Binary will be at ./target/release/openenv-microvm
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
openenv-microvm convert ./envs/echo_env -o echo-env.microvm

# From HuggingFace Space
openenv-microvm convert hf:openenv/echo-env -o echo-env.microvm

# With custom resources
openenv-microvm convert ./envs/echo_env -o echo-env.microvm --memory 512 --vcpus 2
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
openenv-microvm run echo-env.microvm --port 8000

# With custom IP
openenv-microvm run echo-env.microvm --port 8000 --ip 172.16.0.10
```

### Pool management for RL training (Linux only)

```bash
# Start a pool of 32 pre-warmed microVMs
openenv-microvm pool echo-env.microvm --size 32 --port 8000
```

The pool keeps VMs pre-booted for instant acquisition:

```python
from openenv_microvm import MicroVMPool

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

### metadata.json

```json
{
  "version": "1.0",
  "env_name": "echo_env",
  "memory_mb": 256,
  "vcpu_count": 1
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
                              ▼  openenv-microvm convert
┌─────────────────────────────────────────────────────────────┐
│                     MicroVM Package                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ rootfs.ext4 │  │ vmlinux     │  │ config.json         │  │
│  │ (512 MB)    │  │ (26 MB)     │  │ metadata.json       │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼  openenv-microvm run
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

### `openenv-microvm convert`

Convert an OpenEnv environment to a microVM package.

```
USAGE:
    openenv-microvm convert <ENV_PATH> -o <OUTPUT> [OPTIONS]

ARGS:
    <ENV_PATH>    Path to OpenEnv environment directory, or hf:org/repo for HuggingFace

OPTIONS:
    -o, --output <OUTPUT>    Output path for the .microvm package [required]
        --memory <MB>        Memory allocation in MB [default: 256]
        --vcpus <COUNT>      Number of vCPUs [default: 1]
        --kernel <PATH>      Path to custom vmlinux kernel (downloads default if not provided)
    -h, --help               Print help
```

### `openenv-microvm run`

Run a single microVM package. **Requires Linux with KVM.**

```
USAGE:
    openenv-microvm run <PACKAGE_PATH> [OPTIONS]

ARGS:
    <PACKAGE_PATH>    Path to the .microvm package directory

OPTIONS:
    -p, --port <PORT>    Port to expose [default: 8000]
        --ip <IP>        IP address for the VM [default: 172.16.0.2]
    -h, --help           Print help
```

### `openenv-microvm pool`

Run a pool of pre-warmed microVMs. **Requires Linux with KVM.**

```
USAGE:
    openenv-microvm pool <PACKAGE_PATH> [OPTIONS]

ARGS:
    <PACKAGE_PATH>    Path to the .microvm package directory

OPTIONS:
    -n, --size <SIZE>    Number of VMs in the pool [default: 8]
    -p, --port <PORT>    Base port [default: 8000]
    -h, --help           Print help
```

## How It Works

### Convert Process (Docker-based, cross-platform)

1. **Build Container**: Creates a Docker image with Alpine Linux, Python, uvicorn, and your environment code
2. **Install Dependencies**: Runs `pip install -r requirements.txt` if present
3. **Export Filesystem**: Uses `docker export` to create a tarball of the container
4. **Create ext4 Image**: Uses Docker to create and populate an ext4 filesystem image
5. **Download Kernel**: Fetches the Firecracker-compatible Linux kernel from AWS
6. **Generate Config**: Creates the Firecracker configuration JSON

### Runtime (Linux with KVM)

1. **Setup Networking**: Creates a TAP device and configures iptables for NAT
2. **Start Firecracker**: Launches the Firecracker VMM with the config
3. **Boot VM**: Kernel boots and runs `/init.sh` which starts uvicorn
4. **Health Check**: Waits for the `/health` endpoint to respond
5. **Ready**: VM is accessible at `http://<ip>:<port>`

## Performance Comparison

| Metric | Docker Container | Firecracker MicroVM | Improvement |
|--------|------------------|---------------------|-------------|
| Cold start | ~500ms | ~125ms | **4x faster** |
| Memory overhead | ~50MB | ~32MB | **36% less** |
| Reset time | ~200ms | ~50ms | **4x faster** |
| Isolation | Linux namespaces | Hardware (KVM) | **Stronger** |

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
```

## Project Structure

```
src/
├── main.rs      # CLI entry point (clap)
├── convert.rs   # Environment → MicroVM conversion
├── runtime.rs   # Firecracker VM lifecycle management
├── pool.rs      # Pre-warmed VM pool for RL training
└── error.rs     # Error types

tests/
└── integration_tests.rs  # CLI integration tests
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
