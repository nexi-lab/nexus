# Architecture

Nexus = filesystem/context plane.

Choose this path when you need to understand how the kernel, storage pillars, services, and deployment profiles fit together.

## Read These First

- [Kernel Architecture](../architecture/KERNEL-ARCHITECTURE.md)
- [Backend Architecture](../architecture/backend-architecture.md)
- [CLI Design](../architecture/cli-design.md)

## For Deeper Context

- [CLI Experience Design Research](../research/cli-experience-design-research.md)
- [CLI Architecture Proposal](../proposals/cli-architecture-proposal.md)
- [CLI Issues](../proposals/cli-issues.md)
- [Grove Async Agent Graph](../proposals/grove-async-agent-graph.md)

## Why This Path Exists

Nexus has several layers: storage pillars, a VFS-style kernel, system services, and optional bricks. If you are changing those boundaries or explaining the product externally, read the design docs before editing behavior or docs copy.
