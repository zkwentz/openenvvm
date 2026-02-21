# OpenEnv → MicroVM Converter

Convert OpenEnv environments to Firecracker microVMs for ultra-fast startup and secure isolation.

Written in Rust for maximum performance and reliability.

## Why MicroVMs?

| Feature | Docker | MicroVM |
|---------|--------|---------|
| Startup | ~500ms | ~125ms |
| Isolation | Namespace | Hardware |
| Memory | Shared kernel | Dedicated |
| Security | Container escape risk | VM-level isolation |

For RL training at scale, microVMs provide:
- **Faster reset()** — critical for high-throughput training loops
- **Better isolation** — each episode in true hardware isolation
- **Deterministic execution** — no noisy neighbor effects

## Installation

### From crates.io

```bash
cargo install openenv-microvm
```

### From source

```bash
git clone https://github.com/your-org/openenv-microvm
cd openenv-microvm
cargo build --release
```

### Prerequisites

- Linux with KVM support (`/dev/kvm`)
- Firecracker v1.6+
- Docker (for building rootfs)
- Root or `kvm` group membership

```bash
# Install Firecracker
curl -L https://github.com/firecracker-microvm/firecracker/releases/download/v1.6.0/firecracker-v1.6.0-x86_64.tgz | tar xz
sudo mv firecracker-v1.6.0-x86_64 /usr/local/bin/firecracker
```

## Usage

### Convert an OpenEnv environment to MicroVM

```bash
# From local directory
openenv-microvm convert envs/echo_env -o echo-env.microvm

# From HuggingFace Space
openenv-microvm convert hf:openenv/echo-env -o echo-env.microvm

# With custom resources
openenv-microvm convert envs/echo_env -o echo-env.microvm --memory 512 --vcpus 2
```

### Run a MicroVM environment

```bash
# Start the microVM (boots in ~125ms)
openenv-microvm run echo-env.microvm --port 8000

# With custom IP
openenv-microvm run echo-env.microvm --port 8000 --ip 172.16.0.10
```

### Pool management for RL training

```bash
# Start a pool of 32 pre-warmed microVMs
openenv-microvm pool echo-env.microvm --size 32 --port 8000
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    OpenEnv Environment                       │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ models.py   │  │ client.py   │  │ server/             │  │
│  │ (Action,Obs)│  │ (EnvClient) │  │  └─ environment.py  │  │
│  └─────────────┘  └─────────────┘  │  └─ app.py          │  │
│                                    │  └─ Dockerfile      │  │
│                                    └─────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼  openenv-microvm convert
┌─────────────────────────────────────────────────────────────┐
│                     MicroVM Package                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ rootfs.ext4 │  │ vmlinux     │  │ config.json         │  │
│  │ (filesystem)│  │ (kernel)    │  │ (Firecracker spec)  │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼  openenv-microvm run
┌─────────────────────────────────────────────────────────────┐
│                    Firecracker MicroVM                       │
│  ┌─────────────────────────────────────────────────────────┐│
│  │  Linux Kernel (minimal, <5MB)                           ││
│  │  ┌───────────────────────────────────────────────────┐  ││
│  │  │  Alpine rootfs + Python + OpenEnv server          │  ││
│  │  │  └─ uvicorn running environment                   │  ││
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
    <ENV_PATH>    Path to OpenEnv environment or hf:org/repo

OPTIONS:
    -o, --output <OUTPUT>    Output path for the .microvm package [required]
        --memory <MB>        Memory allocation in MB [default: 256]
        --vcpus <COUNT>      Number of vCPUs [default: 1]
        --kernel <PATH>      Path to custom vmlinux kernel
```

### `openenv-microvm run`

Run a single microVM package.

```
USAGE:
    openenv-microvm run <PACKAGE_PATH> [OPTIONS]

ARGS:
    <PACKAGE_PATH>    Path to the .microvm package

OPTIONS:
    -p, --port <PORT>    Port to expose [default: 8000]
        --ip <IP>        IP address for the VM [default: 172.16.0.2]
```

### `openenv-microvm pool`

Run a pool of pre-warmed microVMs.

```
USAGE:
    openenv-microvm pool <PACKAGE_PATH> [OPTIONS]

ARGS:
    <PACKAGE_PATH>    Path to the .microvm package

OPTIONS:
    -n, --size <SIZE>    Pool size [default: 8]
    -p, --port <PORT>    Base port [default: 8000]
```

## Components

### Converter (`src/convert.rs`)
- Resolves environment sources (local paths or HuggingFace)
- Builds minimal Alpine rootfs with Python
- Installs environment dependencies
- Creates ext4 filesystem image
- Generates Firecracker configuration

### Runtime (`src/runtime.rs`)
- Manages Firecracker process lifecycle
- Sets up TAP networking
- Communicates with Firecracker API via Unix socket
- Handles graceful shutdown
- Exposes health checks

### Pool (`src/pool.rs`)
- Pre-boots microVMs for instant acquisition
- Thread-safe pool management with async/await
- Background warmer maintains minimum available VMs
- Handles recycling and cleanup
- Tracks acquisition statistics

## Performance Comparison

| Metric | Docker | MicroVM | Improvement |
|--------|--------|---------|-------------|
| Cold start | 500ms | 125ms | 4x faster |
| Memory overhead | 50MB | 32MB | 36% less |
| Reset time | 200ms | 50ms | 4x faster |
| Isolation | Namespace | KVM | Hardware-level |

## Development

```bash
# Run tests
cargo test

# Run with logging
RUST_LOG=debug cargo run -- convert envs/test -o test.microvm

# Build release binary
cargo build --release
```

## License

MIT
