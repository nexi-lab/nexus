# Quickstart

Nexus = filesystem/context plane.

This page documents the local SDK path re-verified against this repository on
March 11, 2026 using `uv` and Python 3.14. The package metadata supports
Python 3.12+.

## Prerequisites

- `uv`
- Python 3.12 or newer
- A writable local directory for Nexus state

## Set Up A Source Environment

From a checkout:

```bash
uv venv --python 3.14
source .venv/bin/activate
uv pip install -r requirements-minimal.txt
uv pip install -e . --no-deps
```

The remaining commands assume the same activated `.venv`.

## Run A Verified Example

From a checkout:

```bash
python - <<'PY'
from nexus.sdk import connect

nx = connect(
    config={
        "profile": "minimal",
        "data_dir": "./nexus-data",
    }
)

nx.sys_write("/hello.txt", b"Hello, Nexus!")
print(nx.sys_read("/hello.txt").decode())
nx.close()
PY
```

From PyPI, install `nexus-ai-fs` first and run the same snippet without the
source-checkout setup.

Expected output:

```text
Hello, Nexus!
```

## Local CLI Quickstart

This standalone CLI path was also verified in this repository on March 11, 2026.

From a checkout:

```bash
python -m nexus.cli.main init .nexus-cli-demo
export NEXUS_DATA_DIR="$PWD/.nexus-cli-demo/nexus-data"

python -m nexus.cli.main write /workspace/hello.txt "hello from cli"
python -m nexus.cli.main cat /workspace/hello.txt
python -m nexus.cli.main ls /workspace
```

If you installed from PyPI, use `nexus` instead of `python -m nexus.cli.main`.

Expected result:

- `write` succeeds against the workspace-scoped data dir.
- `cat` returns `hello from cli`.
- `ls` shows `/workspace/hello.txt`.

## Optional Capabilities

- Full dev/test environment: `uv sync --extra dev --extra test`
- txtai/FAISS semantic search stack: `uv sync --extra semantic-search`
- Optional Rust acceleration from PyPI: `pip install nexus-kernel`
- Optional Rust acceleration from a checkout: `uv pip install maturin && maturin develop --release -m rust/nexus_runtime/Cargo.toml`
- Rust metastore / federation extensions: `maturin develop --release -m rust/nexus_raft/Cargo.toml --features python` or `--features full`

## Common First-Run Fixes

- `ModuleNotFoundError: No module named 'nexus'`: you skipped the editable install step or are using the wrong interpreter.
- `maturin develop --release` fails at the repo root: point `maturin` at a crate manifest under `rust/`, not the workspace root.
- `maturin develop ... rust/nexus_runtime/Cargo.toml` uses Anaconda or another wrong interpreter: run it from the same activated `.venv` as Nexus. The package metadata requires Python 3.12+.
- `Rust BLAKE3 extension not available`: this is an optional performance message, not a quickstart failure.
- `faiss-cpu` resolution fails: the default quickstart above avoids the optional semantic-search stack; only opt into `semantic-search` on platforms with compatible `txtai` and `faiss-cpu` wheels.

## Why This Quickstart

- It does not require `nexusd`.
- It does not require API keys.
- It does not require the full federation build.
- It keeps federation-by-default intact while letting local fallback happen automatically when Rust extensions are absent.
- It exercises the real local `connect()` path used by the SDK and CLI.

## Next Steps

- Stay local and keep building from the [Local SDK path](../paths/embedded-sdk.md).
- Move to a service deployment with the [Shared daemon path](../paths/daemon-and-remote.md).
- Read the system model in [Architecture](../paths/architecture.md).
- Read the [trust boundary guide](trust-boundary.md).
