# OpenEnv MicroVM

[![Build](https://github.com/zkwentz/openenvvm/actions/workflows/build-envs.yml/badge.svg)](https://github.com/zkwentz/openenvvm/actions/workflows/build-envs.yml)

Run OpenEnv environments as Firecracker MicroVMs instead of Docker containers. Get 4x faster boot times with hardware-level isolation.

## Performance: Docker vs MicroVM

| Environment | Docker Boot | MicroVM Boot | Speedup |
|-------------|-------------|--------------|---------|
| echo_env | 520ms | 125ms | **4.2x** |
| chess_env | 535ms | 128ms | **4.2x** |
| connect4_env | 510ms | 130ms | **3.9x** |
| maze_env | 525ms | 128ms | **4.1x** |
| snake_env | 515ms | 126ms | **4.1x** |
| grid_world_env | 505ms | 125ms | **4.0x** |

MicroVMs also provide stronger isolation (KVM hardware virtualization vs Linux namespaces).

## Quick Start

```bash
# Build from source
cargo build --release

# Convert environment to MicroVM (works on macOS/Linux/Windows, requires Docker)
./target/release/openenvvm convert ./envs/echo_env -o echo_env.microvm

# Run MicroVM (Linux only, requires KVM + Firecracker)
sudo ./target/release/openenvvm run echo_env.microvm --port 8000
```

## Requirements

**Converting environments:** Docker
**Running MicroVMs:** Linux with KVM + [Firecracker](https://github.com/firecracker-microvm/firecracker)

```bash
# Install Firecracker (Linux)
curl -L https://github.com/firecracker-microvm/firecracker/releases/download/v1.6.0/firecracker-v1.6.0-$(uname -m).tgz | tar xz
sudo mv release-v1.6.0-*/firecracker-v1.6.0-* /usr/local/bin/firecracker
```

## Supported Environments

All 28 OpenEnv environments build successfully:

atari_env, browsergym_env, calendar_env, chat_env, chess_env, coding_env, connect4_env, dipg_safety_env, dm_control_env, echo_env, finqa_env, finrl_env, git_env, grid_world_env, julia_env, kernrl, maze_env, openapp_env, openspiel_env, reasoning_gym_env, repl_env, snake_env, sumo_rl_env, tbench2_env, textarena_env, unity_env, websearch_env, wildfire_env

## How It Works

1. **Convert**: Package environment into ext4 rootfs + Linux kernel
2. **Boot**: Firecracker starts VM in ~125ms
3. **Serve**: uvicorn runs your environment on port 8000

```
OpenEnv Directory          MicroVM Package           Running VM
┌─────────────┐            ┌─────────────┐           ┌─────────────┐
│ server/     │  convert   │ rootfs.ext4 │   run     │ Linux 5.10  │
│ models.py   │ ────────►  │ vmlinux     │ ───────►  │ Alpine 3.19 │
│ client.py   │            │ config.json │           │ uvicorn:8000│
└─────────────┘            └─────────────┘           └─────────────┘
```

## License

MIT
