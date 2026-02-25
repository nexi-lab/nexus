# ADR: NexusFS Method Freeze — No New Methods on the God Object

**Issue**: [#1519](https://github.com/nexi-lab/nexus/issues/1519)
**Status**: Accepted
**Date**: 2026-02-16

## Summary

NexusFS (`core/nexus_fs.py`) is frozen for new public methods. All new
functionality must be implemented as extracted services or subsystems
following the pattern established in Issue #1287.

## Background

NexusFS has grown into a god object:

- **188+ RPC-exposed methods** spanning filesystem, auth, search, memory,
  MCP, OAuth, governance, skills, events, and more
- **10,700+ lines** across `nexus_fs.py` and `nexus_fs_core.py`
- **10+ service domains** conflated into one class
- New features added directly to NexusFS create coupling, increase test
  surface, and make the kernel harder to reason about

Issue #1287 established the extraction pattern: move domain logic into
services (`src/nexus/services/`) and subsystems (`src/nexus/core/subsystems/`),
leaving NexusFS as a thin delegation layer.

## Decision

**NexusFS is frozen for new public methods.**

### Rules

1. **No new methods on NexusFS or NexusFSCoreMixin** unless they are:
   - Kernel primitives (inode CRUD: read, write, mkdir, delete, stat, list)
   - Thin delegation methods (1-3 lines forwarding to an extracted service)

2. **New features must be implemented as**:
   - **Services** in `src/nexus/services/` (for domain logic: auth, search, memory, etc.)
   - **Subsystems** in `src/nexus/core/subsystems/` (for kernel-adjacent concerns)
   - **Protocols** in `src/nexus/services/protocols/` (for cross-brick contracts)

3. **Delegation pattern** for RPC compatibility:
   ```python
   # In nexus_fs.py — thin delegation only
   @rpc_expose(description="Search memory")
   async def memory_search(self, query: str, **kwargs) -> list[dict]:
       return await self.memory_service.search(query, **kwargs)
   ```

4. **Existing methods may be refactored** to delegate to services but
   must preserve the RPC signature (method name + params).

### Extraction Template (from Issue #1287)

1. Write tests for the new service (compliance + specific behavior)
2. Create service in `src/nexus/services/` or subsystem in `core/subsystems/`
3. If migrating from NexusFS: remove logic from the mixin, keep the
   `@rpc_expose` method as a 1-line delegation
4. Wire via `_wire_services()` (accepts pre-built services from KernelServices)
5. Update remote NexusFS (via `nexus.connect()`) if needed
6. Deprecate old mixin file if applicable

## Consequences

### Positive

- **Bounded complexity**: NexusFS method count stabilizes at ~188
- **Testability**: Services are unit-testable in isolation
- **Composability**: Services can be reused without NexusFS (e.g., CLI tools)
- **Onboarding**: New contributors add services, not methods to a 10K-line file

### Negative

- **Migration cost**: Existing methods still live in NexusFS until extracted
- **Delegation overhead**: Thin wrappers add a small amount of boilerplate
- **Coordination**: Developers must understand the service extraction pattern

## References

- Issue #1287: Extract NexusFS Domain Services (established the pattern)
- Issue #1519: Fix architecture contract violations (enforces the boundary)
- `docs/design/KERNEL-ARCHITECTURE.md`: Four-pillar storage model
- `src/nexus/services/protocols/__init__.py`: Protocol conventions
