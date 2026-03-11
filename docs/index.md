# Nexus

Nexus = filesystem/context plane.

Nexus gives agents a stable place to read, write, search, and carry context across files, services, and runs. Start with the smallest path that matches your use case, then move up to daemon-backed deployments or architecture docs when you need them.

## Start Here

### 1. Local SDK

Use Nexus inside a Python process with a local data directory. This is the shortest verified path and the best place to start, using the `uv`-based quickstart from a source checkout or PyPI install.

- Docs: [Local SDK](paths/embedded-sdk.md)
- Verified guide: [Quickstart](getting-started/quickstart.md)

### 2. Shared Daemon

Run `nexusd` when you need a long-lived service, remote clients, or operational controls. The remote SDK path depends on a configured gRPC port in addition to the HTTP URL.

- Docs: [Shared Daemon](paths/daemon-and-remote.md)

### 3. Architecture

Read the design docs before changing the storage model, service boundaries, or deployment assumptions.

- Docs: [Architecture](paths/architecture.md)
- Deep dive: [Kernel Architecture](architecture/KERNEL-ARCHITECTURE.md)

## What To Trust

- The quickstart in this docsite is a local embedded path that was verified against this repository.
- Remote SDK access is a separate path. It requires `nexusd` and a configured gRPC port.
- Permissions, memory, and federation are deployment capabilities, not implied by the basic local write/read example.

## Links

- GitHub: <https://github.com/nexi-lab/nexus>
- PyPI: <https://pypi.org/project/nexus-ai-fs/>
- Examples: <https://github.com/nexi-lab/nexus/tree/main/examples>
