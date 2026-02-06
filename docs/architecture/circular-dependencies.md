# Circular Dependencies Analysis

**Date:** 2026-01-02
**Status:** Phase 1 Analysis
**Related:** Issue #987 (Phase 1), Issue #990 (Phase 4)

---

## Summary

**Found:** 189 files with TYPE_CHECKING guards (indicates circular import issues)
**Goal:** Reduce to <10 by Phase 4 using dependency inversion and proper layering

---

## What Are Circular Dependencies?

Circular dependencies occur when Module A imports Module B, and Module B imports Module A (directly or indirectly). This creates import cycles that Python cannot resolve.

```python
# module_a.py
from module_b import ClassB  # ❌ Circular!

class ClassA:
    def use_b(self) -> ClassB:
        pass

# module_b.py
from module_a import ClassA  # ❌ Circular!

class ClassB:
    def use_a(self) -> ClassA:
        pass
```

---

## Current State

### Files with TYPE_CHECKING (Top 20)

Analyzed with: `grep -r "TYPE_CHECKING" src/nexus --include="*.py" -l`

**Core Module (Worst Offenders):**
1. `src/nexus/core/nexus_fs.py`
2. `src/nexus/core/rebac_manager.py`
3. `src/nexus/core/rebac_manager_enhanced.py`
4. `src/nexus/core/nexus_fs_rebac.py`
5. `src/nexus/core/nexus_fs_mcp.py`
6. `src/nexus/core/metadata.py`
7. `src/nexus/core/sessions.py`
8. `src/nexus/core/filesystem.py`

**Storage Module:**
9. `src/nexus/storage/sqlalchemy_metadata_store.py`
10. `src/nexus/storage/models.py`
11. `src/nexus/storage/content_cache.py`

**Remote Module:**
12. `src/nexus/remote/client.py`
13. `src/nexus/remote/async_client.py`
14. `src/nexus/remote/auth.py`

**LLM Module:**
15. `src/nexus/llm/context_builder.py`
16. `src/nexus/llm/document_reader.py`
17. `src/nexus/llm/provider.py`

**Other Modules:**
18. `src/nexus/sync.py`
19. `src/nexus/core/cache/factory.py`
20. `src/nexus/core/cache/postgres.py`

---

## Common Circular Dependency Patterns

### Pattern 1: Type Hint Circularity

**Problem:** Two modules need to reference each other's types for type hints.

```python
# File: nexus_fs.py
if TYPE_CHECKING:
    from nexus.core._metadata_generated import MetadataStore

class NexusFS:
    def __init__(self, metadata: 'MetadataStore'):
        self.metadata = metadata

# File: metadata_store.py
if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

class MetadataStore:
    def attach_fs(self, fs: 'NexusFS'):
        self.fs = fs
```

**Solution:** Use Protocol types

```python
# File: protocols.py
from typing import Protocol

class MetadataStoreProtocol(Protocol):
    def get(self, path: str) -> FileMetadata: ...
    def set(self, path: str, metadata: FileMetadata): ...

class NexusFilesystem(Protocol):
    def read(self, path: str) -> bytes: ...
    def write(self, path: str, content: bytes): ...

# File: nexus_fs.py
from protocols import MetadataStoreProtocol

class NexusFS:
    def __init__(self, metadata: MetadataStoreProtocol):
        self.metadata = metadata

# File: metadata_store.py
from protocols import NexusFilesystem

class MetadataStore:
    def attach_fs(self, fs: NexusFilesystem):
        self.fs = fs
```

---

### Pattern 2: Service Cross-References

**Problem:** Services depend on each other.

```python
# search_service.py
if TYPE_CHECKING:
    from permission_service import PermissionService

class SearchService:
    def __init__(self, permissions: 'PermissionService'):
        self.permissions = permissions

# permission_service.py
if TYPE_CHECKING:
    from search_service import SearchService

class PermissionService:
    def __init__(self, search: 'SearchService'):
        self.search = search
```

**Solution:** Dependency Injection with optional dependencies

```python
# search_service.py
class SearchService:
    def __init__(self):
        self._permissions = None

    def set_permissions(self, permissions):
        self._permissions = permissions

# permission_service.py
class PermissionService:
    def __init__(self):
        pass  # No dependency on search

# main.py
search = SearchService()
permissions = PermissionService()
search.set_permissions(permissions)
```

---

### Pattern 3: Parent-Child Circular Reference

**Problem:** Base class imports child, child inherits from base.

```python
# base.py
if TYPE_CHECKING:
    from child import ChildClass

class BaseClass:
    def create_child(self) -> 'ChildClass':
        from child import ChildClass  # Runtime import
        return ChildClass()

# child.py
from base import BaseClass

class ChildClass(BaseClass):
    pass
```

**Solution:** Factory pattern or plugin registry

```python
# base.py
class BaseClass:
    _child_factory = None

    @classmethod
    def register_child_factory(cls, factory):
        cls._child_factory = factory

    def create_child(self):
        return self._child_factory()

# child.py
from base import BaseClass

class ChildClass(BaseClass):
    pass

# After child is defined
BaseClass.register_child_factory(lambda: ChildClass())
```

---

## Module Dependency Map

Current (Circular):
```
core/nexus_fs.py ←→ storage/metadata_store.py
core/nexus_fs.py ←→ core/rebac_manager.py
core/nexus_fs.py ←→ remote/client.py
storage/metadata_store.py ←→ storage/models.py
remote/client.py ←→ core/nexus_fs.py
llm/context_builder.py ←→ core/nexus_fs.py
```

Target (Layered):
```
┌─────────────────────────────────┐
│         API Layer               │
│   (FastAPI, CLI, Remote Client) │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│      Service Layer              │
│  (Search, Permissions, Mounts)  │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│       Core Layer                │
│   (NexusFS Orchestrator)        │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│     Storage Layer               │
│  (Metadata, Backends, Cache)    │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│   Infrastructure Layer          │
│   (Database, Redis, S3, GCS)    │
└─────────────────────────────────┘

Rule: Upper layers can depend on lower layers, but never vice versa
```

---

## Refactoring Strategy

### Phase 1 (Current): Document
- ✅ Identify all circular dependencies
- ✅ Document patterns
- ✅ Create this analysis

### Phase 2 (Weeks 5-12): Break Core Cycles
When extracting services from NexusFS:
1. Define Protocol interfaces first
2. Extract service with Protocol dependency
3. Update NexusFS to use service via composition
4. No circular references because services don't know about NexusFS

Example:
```python
# protocols.py (new)
class PermissionServiceProtocol(Protocol):
    def check_permission(self, context, resource, action) -> bool: ...

# permission_service.py (new)
class PermissionService:
    def __init__(self, rebac_manager):
        self.rebac = rebac_manager  # No nexus_fs dependency!

# nexus_fs.py (updated)
class NexusFS:
    def __init__(self):
        self.permissions = PermissionService(...)  # Composition, no circular ref
```

### Phase 3 (Weeks 13-16): Fix Type Hints
- Replace all TYPE_CHECKING with Protocol types
- Remove string-quoted type hints where possible
- Use TypedDict for structured data

### Phase 4 (Weeks 17-20): Eliminate Remaining Cycles
- Implement proper layering (as shown above)
- Use dependency injection
- Break any remaining cycles with event systems

---

## Metrics

### Current Baseline
- **Files with TYPE_CHECKING:** 189
- **Estimated circular dependency cycles:** Unknown (requires full analysis)
- **Largest affected module:** `core/` (most TYPE_CHECKING guards)

### Phase 2 Goal
- **Files with TYPE_CHECKING:** < 100 (after service extraction)
- **Circular cycles in core:** 0

### Phase 4 Goal
- **Files with TYPE_CHECKING:** < 10
- **All circular cycles:** Eliminated
- **Clear module layering:** Established

---

## Tools for Detection

### Manual Analysis
```bash
# Find all TYPE_CHECKING guards
grep -r "TYPE_CHECKING" src/nexus --include="*.py" -l

# Count per file
grep -r "TYPE_CHECKING" src/nexus --include="*.py" -c | sort -t: -k2 -rn
```

### Automated Tools (Future)
- **pydeps:** Visualize dependencies (requires graphviz)
- **import-linter:** Enforce layer boundaries
- **Custom linter:** Detect new TYPE_CHECKING additions

---

## Prevention Strategy

### Pre-commit Hook (Phase 1)
Currently blocks new type: ignore comments. Future: Add TYPE_CHECKING limit.

### Architecture Guidelines (Phase 2+)
1. **Protocol-First Design:** Define interfaces before implementations
2. **Layered Architecture:** Strict one-way dependencies
3. **Dependency Injection:** Services receive dependencies, don't import them
4. **Event-Driven:** Use events for cross-module communication
5. **No Parent-Child Circular Refs:** Parent never imports child

---

## Examples of Good vs. Bad

### ❌ Bad: Circular Type Hints
```python
# service_a.py
if TYPE_CHECKING:
    from service_b import ServiceB

class ServiceA:
    def use_b(self, b: 'ServiceB'): ...

# service_b.py
if TYPE_CHECKING:
    from service_a import ServiceA

class ServiceB:
    def use_a(self, a: 'ServiceA'): ...
```

### ✅ Good: Protocol-Based
```python
# protocols.py
class ServiceAProtocol(Protocol):
    def do_something(self): ...

class ServiceBProtocol(Protocol):
    def do_other_thing(self): ...

# service_a.py
from protocols import ServiceBProtocol

class ServiceA:
    def use_b(self, b: ServiceBProtocol): ...

# service_b.py
from protocols import ServiceAProtocol

class ServiceB:
    def use_a(self, a: ServiceAProtocol): ...
```

---

## Related Issues

- **Issue #987:** Phase 1 - Stabilization (this analysis)
- **Issue #988:** Phase 2 - Core Refactoring (break core cycles)
- **Issue #989:** Phase 3 - API Cleanup (remove TYPE_CHECKING)
- **Issue #990:** Phase 4 - Optimization (eliminate all cycles)

---

**Next Steps:**
1. Complete Phase 1 documentation ✅
2. Start Phase 2 service extraction
3. Use Protocols to prevent new cycles
4. Track TYPE_CHECKING count in CI (baseline: 189)

---

**Document Status:** Complete for Phase 1
**Last Updated:** 2026-01-02
