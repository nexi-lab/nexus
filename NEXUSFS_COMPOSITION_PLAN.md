# NexusFS Service Composition Plan

**Goal:** Wire 8 extracted services into NexusFS, replacing mixin inheritance with service composition

**Date:** 2026-01-03
**Status:** Planning

---

## Current Architecture (Phase 1)

```python
class NexusFS(
    NexusFSCoreMixin,          # Core file operations
    NexusFSSearchMixin,        # Search operations → SearchService (partial)
    NexusFSReBACMixin,         # Permission operations → ReBACService
    NexusFSVersionsMixin,      # Version operations → VersionService
    NexusFSMountsMixin,        # Mount operations → MountService
    NexusFSOAuthMixin,         # OAuth operations → OAuthService
    NexusFSSkillsMixin,        # Skill operations → SkillService
    NexusFSMCPMixin,           # MCP operations → MCPService
    NexusFSLLMMixin,           # LLM operations → LLMService
    NexusFilesystem,           # Base filesystem
):
    """God Object with 9+ mixins (anti-pattern)"""
```

### Problems with Current Design
1. **God Object**: 9 mixins, 100+ methods, ~15,000 lines
2. **Hidden Dependencies**: Mixins access NexusFS internals via `self`
3. **Hard to Test**: Must instantiate entire NexusFS to test one feature
4. **Circular Imports**: TYPE_CHECKING hacks everywhere
5. **Poor Separation**: Business logic mixed with infrastructure

---

## Target Architecture (Phase 2)

```python
class NexusFS(NexusFilesystem):
    """Lean filesystem coordinating independent services via composition"""

    def __init__(self, ...):
        # Core infrastructure (unchanged)
        self.backend = backend
        self.metadata = SQLAlchemyMetadataStore(...)
        self.router = PathRouter()
        self._rebac_manager = EnhancedReBACManager(...)
        self._permission_enforcer = PermissionEnforcer(...)

        # NEW: Service composition (Phase 2)
        self.version_service = VersionService(
            metadata_store=self.metadata,
            cas_store=self.backend,  # CAS operations
            router=self.router,
        )

        self.rebac_service = ReBACService(
            rebac_manager=self._rebac_manager,
            enforce_permissions=enforce_permissions,
        )

        self.mount_service = MountService(router=self.router)
        self.mcp_service = MCPService(nexus_fs=self)
        self.llm_service = LLMService(nexus_fs=self)
        self.oauth_service = OAuthService(
            oauth_factory=None,  # Lazy init
            token_manager=None,  # Lazy init
        )
        self.skill_service = SkillService(nexus_fs=self)

        self.search_service = SearchService(
            metadata_store=self.metadata,
            permission_enforcer=self._permission_enforcer,
            enforce_permissions=enforce_permissions,
        )

    # Delegate mixin methods to services
    def list_versions(self, path: str, ...) -> list[dict]:
        """Delegate to VersionService."""
        return await self.version_service.list_versions(path, ...)

    def rebac_check(self, ...) -> bool:
        """Delegate to ReBACService."""
        return await self.rebac_service.rebac_check(...)

    # ... (60+ delegation methods)
```

### Benefits of New Design
1. **Single Responsibility**: Each service owns one feature
2. **Testability**: Services are independently testable
3. **Explicit Dependencies**: Constructor injection, no hidden `self` access
4. **Type Safety**: No circular imports, cleaner type hints
5. **Gradual Migration**: Can coexist with mixins during transition

---

## Implementation Strategy

### Phase 2.1: Add Service Instances (This Task)

**Goal**: Instantiate services in NexusFS.__init__() without breaking anything

#### Step 1: Add Service Imports
```python
# In nexus/core/nexus_fs.py
from nexus.services.version_service import VersionService
from nexus.services.rebac_service import ReBACService
from nexus.services.mount_service import MountService
from nexus.services.mcp_service import MCPService
from nexus.services.llm_service import LLMService
from nexus.services.oauth_service import OAuthService
from nexus.services.skill_service import SkillService
from nexus.services.search_service import SearchService
```

#### Step 2: Instantiate Services (After Line ~398)
```python
def __init__(self, ...):
    # ... existing initialization (lines 76-398)

    # Phase 2: Service Composition - Extract from mixins into services
    # These services are independent, testable, and follow single-responsibility principle

    # VersionService: File versioning operations (4 methods)
    self.version_service = VersionService(
        metadata_store=self.metadata,
        cas_store=self.backend,  # For CAS operations
        router=self.router,
        enforce_permissions=enforce_permissions,
    )

    # ReBACService: Permission and access control operations (12 core + 15 advanced methods)
    self.rebac_service = ReBACService(
        rebac_manager=self._rebac_manager,
        enforce_permissions=enforce_permissions,
        enable_audit_logging=True,  # Production default
    )

    # MountService: Dynamic backend mounting operations (17 methods)
    self.mount_service = MountService(
        router=self.router,
        mount_manager=self.mount_manager,  # Persistent storage
        rebac_manager=self._rebac_manager,  # For ownership grants
    )

    # MCPService: Model Context Protocol operations (5 methods)
    self.mcp_service = MCPService(nexus_fs=self)

    # LLMService: LLM integration operations (4 methods)
    self.llm_service = LLMService(nexus_fs=self)

    # OAuthService: OAuth authentication operations (7 methods)
    self.oauth_service = OAuthService(
        oauth_factory=None,  # Lazy init from config
        token_manager=None,  # Lazy init from db_path
    )

    # SkillService: Skill management operations (16 methods)
    self.skill_service = SkillService(nexus_fs=self)

    # SearchService: Search operations - semantic (4) + basic (3, deferred to Phase 2.2)
    self.search_service = SearchService(
        metadata_store=self.metadata,
        permission_enforcer=self._permission_enforcer,
        enforce_permissions=enforce_permissions,
    )
```

**Testing**: Run existing tests - should pass since mixins still active

---

### Phase 2.2: Implement Delegation Methods

**Goal**: Add delegation methods to NexusFS that forward calls to services

#### Option A: Manual Delegation (Explicit, Verbose)
```python
# In NexusFS class
async def list_versions(
    self,
    path: str,
    limit: int = 100,
    offset: int = 0,
    context: OperationContext | None = None,
) -> list[dict[str, Any]]:
    """List file versions - delegates to VersionService."""
    return await self.version_service.list_versions(path, limit, offset, context)

async def get_version(
    self,
    path: str,
    version_id: str,
    context: OperationContext | None = None,
) -> dict[str, Any] | None:
    """Get version details - delegates to VersionService."""
    return await self.version_service.get_version(path, version_id, context)

# Repeat for 60+ methods...
```

**Pros**: Clear, explicit, easy to debug
**Cons**: Verbose (60+ methods), maintenance burden

#### Option B: __getattr__ Magic (DRY, Implicit)
```python
# In NexusFS class
_SERVICE_METHOD_MAP = {
    # VersionService methods
    "list_versions": "version_service",
    "get_version": "version_service",
    "rollback_to_version": "version_service",
    "delete_version": "version_service",

    # ReBACService methods
    "rebac_create": "rebac_service",
    "rebac_check": "rebac_service",
    "rebac_expand": "rebac_service",
    # ... (60+ mappings)
}

def __getattr__(self, name: str) -> Any:
    """Delegate method calls to appropriate service."""
    if name in self._SERVICE_METHOD_MAP:
        service_name = self._SERVICE_METHOD_MAP[name]
        service = getattr(self, service_name)
        return getattr(service, name)
    raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")
```

**Pros**: DRY, automatic delegation
**Cons**: Magic, harder to debug, IDE autocomplete broken

**Decision**: Use **Option A (Manual Delegation)** for clarity and IDE support

---

### Phase 2.3: Remove Mixin Inheritance

**Goal**: Delete mixin classes after delegation methods are in place

```python
# Before
class NexusFS(
    NexusFSCoreMixin,
    NexusFSSearchMixin,
    NexusFSReBACMixin,
    # ... 9 mixins
    NexusFilesystem,
):

# After
class NexusFS(NexusFilesystem):
    """Lean filesystem using service composition."""
```

**Testing**: Comprehensive test suite must pass

---

### Phase 2.4: Complete SearchService Basic Operations

**Goal**: Migrate list(), glob(), grep() from NexusFSSearchMixin to SearchService

**Deferred Until**: After composition wiring complete (these methods need self.metadata access)

---

## Dependency Injection Pattern

### Services with External Dependencies
```python
# These services need infrastructure components
VersionService(metadata_store, cas_store, router)
ReBACService(rebac_manager, enforce_permissions)
MountService(router, mount_manager, rebac_manager)
SearchService(metadata_store, permission_enforcer, enforce_permissions)
```

### Services with NexusFS Dependency
```python
# These services need full NexusFS for file operations
MCPService(nexus_fs=self)  # Needs read/write for MCP tools
LLMService(nexus_fs=self)  # Needs read for context
SkillService(nexus_fs=self)  # Needs read/write for skill files
```

**Note**: Services that take `nexus_fs` will still work because they only call public API methods (read/write/list), not mixin internals

---

## Testing Strategy

### Level 1: Unit Tests (Already Done)
- ✅ Smoke tests for all 8 services
- ⏳ Comprehensive unit tests (ongoing)

### Level 2: Integration Tests (This Task)
1. **Service instantiation test**: Verify all services created successfully
2. **Delegation test**: Call each delegated method, verify service method called
3. **Backward compatibility test**: Existing NexusFS tests should pass

### Level 3: End-to-End Tests
- Run full test suite (`pytest tests/`)
- Run RPC server integration tests
- Manual smoke testing with CLI

---

## Rollout Plan

### Step 1: Service Instantiation (Low Risk)
- Add service instances to __init__
- Don't touch mixins yet
- Deploy to dev, verify no breakage

### Step 2: Add Delegation Methods (Medium Risk)
- Add delegation methods to NexusFS
- Shadow mixin methods (both present)
- Run A/B testing (call both, compare results)

### Step 3: Remove Mixins (High Risk)
- Delete mixin inheritance
- Delete mixin files
- Full test suite must pass
- Deploy gradually (canary → staging → prod)

---

## File Modifications Required

### Files to Modify
1. `src/nexus/core/nexus_fs.py` (~400 LOC changes)
   - Add 8 service imports
   - Add 8 service instantiations in __init__
   - Add 60+ delegation methods

### Files to Delete (Phase 2.3)
1. `src/nexus/core/nexus_fs_versions.py` (VersionsMixin)
2. `src/nexus/core/nexus_fs_mounts.py` (MountsMixin)
3. `src/nexus/core/nexus_fs_oauth.py` (OAuthMixin)
4. `src/nexus/core/nexus_fs_skills.py` (SkillsMixin)
5. `src/nexus/core/nexus_fs_mcp.py` (MCPMixin)
6. `src/nexus/core/nexus_fs_llm.py` (LLMMixin)
7. `src/nexus/core/nexus_fs_rebac.py` (ReBACMixin - partial, keep namespace methods)
8. `src/nexus/core/nexus_fs_search.py` (SearchMixin - partial, semantic methods only)

### Files NOT to Delete (Yet)
- `src/nexus/core/nexus_fs_core.py` (CoreMixin) - Contains essential file operations

---

## Risk Assessment

### 🟢 Low Risk
- Service instantiation - additive, no behavior changes
- Smoke tests already passing

### 🟡 Medium Risk
- Delegation methods - could introduce bugs if signatures don't match
- OAuth lazy initialization - may need special handling

### 🔴 High Risk
- Removing mixin inheritance - could break existing code
- SearchService basic ops - touches core operations (list/glob/grep)

---

## Success Criteria

1. ✅ All services instantiate successfully in NexusFS.__init__
2. ✅ All delegation methods forward correctly to services
3. ✅ Existing test suite passes (backward compatibility)
4. ✅ RPC server integration tests pass
5. ✅ No performance regression (benchmark critical paths)
6. ✅ Type checking passes (mypy)
7. ✅ Linting passes (ruff)

---

## Next Steps

1. **Implement service instantiation** in NexusFS.__init__() (Phase 2.1)
2. **Add delegation methods** for all 60+ service methods (Phase 2.2)
3. **Run integration tests** to verify backward compatibility
4. **Monitor CI/CD** for any failures
5. **Prepare for mixin removal** (Phase 2.3) after validation

---

**Last Updated**: 2026-01-03
