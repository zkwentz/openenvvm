# OpenEnv Environment Compatibility

Status of all 28 OpenEnv environments when converted to Firecracker MicroVMs using the generic conversion pipeline.

The conversion pipeline uses `python:3.11-slim-bookworm` (Debian/glibc) as the rootfs base, installs dependencies from `requirements.txt` and `pyproject.toml`, and runs the server via `uvicorn env.server.app:app`.

## Fully Working (11/28)

These environments convert, boot, and pass full validation (health + reset + step/MCP tool calls).

| Environment | Type | Notes |
|---|---|---|
| atari_env | RL | ale-py installs from glibc wheel |
| calendar_env | MCP | SQLAlchemy backend |
| coding_env | Code execution | smolagents dependency |
| connect4_env | RL | Pure Python |
| echo_env | MCP | Pure Python |
| grid_world_env | RL | Pure Python |
| julia_env | RL | Pure Python |
| maze_env | RL | Pure Python |
| reasoning_gym_env | RL | reasoning-gym pip package |
| repl_env | MCP | Pure Python |
| wildfire_env | RL | pyproject.toml-only install |

## Build Failures (4/28)

These environments fail during the Docker build phase due to resource constraints on CI runners (disk space, memory, or build time). They would likely work on machines with more resources.

| Environment | Blocker | What Needs to Change |
|---|---|---|
| browsergym_env | Playwright downloads ~2GB of browser binaries during `pip install` | Needs a CI runner with >20GB disk, or a pre-built base image with browsers |
| chat_env | PyTorch is ~2GB and exceeds CI runner disk during Docker build | Needs a CI runner with >20GB disk, or a pre-built PyTorch base image |
| kernrl | PyTorch + Triton (~3GB combined) exceed CI runner resources | Same as chat_env |
| openapp_env | Playwright browser binary downloads (same as browsergym_env) | Same as browsergym_env |

## Validation Failures (13/28)

These environments build successfully but the server fails to start. Root causes fall into distinct categories.

### Missing Dependency Files (3)

These environments have no `requirements.txt` or `pyproject.toml`, so their Python dependencies are never installed. The upstream OpenEnv repo relies on custom Docker base images to provide dependencies.

| Environment | Missing Dependencies | What Needs to Change in OpenEnv |
|---|---|---|
| chess_env | `python-chess`, `moonfish` | Add a `pyproject.toml` or `server/requirements.txt` listing dependencies |
| finrl_env | `finrl`, `stable-baselines3`, `pandas`, `yfinance`, etc. | Add a `pyproject.toml` listing dependencies (currently only in Dockerfile `pip install` commands) |
| git_env | None missing, but requires Gitea service | Add a `pyproject.toml` even if empty, or document the external service requirement |

### Missing External Services (2)

These environments crash at module load because they validate required environment variables pointing to external services that aren't available inside the MicroVM.

| Environment | Required Service | What Needs to Change in OpenEnv |
|---|---|---|
| dipg_safety_env | `DIPG_DATASET_PATH` env var (raises `RuntimeError` at import time) | Defer dataset path validation to `reset()` instead of module scope, or provide a default/mock mode |
| git_env | `GITEA_URL`, `GITEA_USERNAME`, `GITEA_PASSWORD` env vars (raises `RuntimeError` at import time) | Defer Gitea connection validation to `reset()` instead of module scope, or support a local git mode |

### Missing System-Level Dependencies (4)

These environments need system packages or native binaries that aren't installed by the generic conversion pipeline and can't be `pip install`ed.

| Environment | Missing System Dependency | What Needs to Change in OpenEnv |
|---|---|---|
| dm_control_env | MuJoCo requires OpenGL libraries (`libosmesa6`, `libgl1`) and `MUJOCO_GL=osmesa` env var | Document system requirements in `pyproject.toml` metadata or provide a setup script |
| openspiel_env | `pyspiel` must be compiled from C++ source (~30 min build from github.com/google-deepmind/open_spiel) | Publish a pre-built `pyspiel` wheel to PyPI, or document the build requirement |
| snake_env | `gym==0.24.1` (legacy) fails to build; `marlenv` depends on it | Migrate from `gym==0.24.1` to `gymnasium` (the maintained fork) |
| sumo_rl_env | Requires `sumo` and `sumo-tools` system binaries (SUMO traffic simulator) | Document the `apt-get install sumo sumo-tools` requirement or provide a setup script |

### Dependencies Too Large for Standard Rootfs (2)

These environments have dependency trees that exceed reasonable rootfs sizes (~4GB) due to large ML frameworks.

| Environment | Large Dependency | Approximate Size |
|---|---|---|
| tbench2_env | `camel-ai` pulls PyTorch, transformers, and full ML stack | ~10GB |
| unity_env | `mlagents-envs` pulls `grpcio`, `protobuf`, plus needs a compiled Unity game binary (not shipped) | ~5GB + external binary |

### Import/Namespace Issues (2)

These environments fail at import time due to Python namespace issues between `openenv-core` (legacy) and `openenv` (current).

| Environment | Issue | What Needs to Change in OpenEnv |
|---|---|---|
| websearch_env | `app.py` imports from `openenv_core.env_server.http_server` instead of `openenv.core` | Update imports to use `openenv.core` namespace |
| finqa_env | `pyproject.toml` lists `fastmcp>=2.0.0` but install may fail silently; `openenv-core[core]` dependency can't resolve | Update `openenv-core[core]` to `openenv[core]` in pyproject.toml |

### Other (1)

| Environment | Issue | What Needs to Change in OpenEnv |
|---|---|---|
| textarena_env | `gradio>=4.0.0` and `textarena>=0.6.1` are large; NLTK data download may fail without internet access | Pre-download NLTK data during build, or disable with `TEXTARENA_DOWNLOAD_NLTK=0` |

## Summary of Changes Needed in OpenEnv

For maximum compatibility with any generic conversion tool (not just openenvvm), OpenEnv environments should:

1. **Always include a `pyproject.toml` or `requirements.txt`** listing all Python dependencies. Don't rely on Docker base images to provide them.

2. **Never validate external service connections at module scope.** Environment variables and service URLs should be checked lazily (in `reset()` or `__init__`), not at import time. Module-scope `raise RuntimeError` prevents uvicorn from even starting.

3. **Use the `openenv.core` namespace** (not the deprecated `openenv_core`). The compatibility shim doesn't reliably forward all submodules.

4. **Document system-level requirements** (native libraries, compiled binaries) in a standard way so conversion tools can install them. A `system-requirements.txt` or metadata field in `pyproject.toml` would work.

5. **Migrate from `gym` to `gymnasium`**. The legacy `gym==0.24.1` package is unmaintained and has build compatibility issues on modern systems.
