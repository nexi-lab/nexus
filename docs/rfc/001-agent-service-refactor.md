# RFC-001: Agent Service Refactoring

**Status:** Draft
**Author:** Claude
**Created:** 2026-01-05
**Related Issues:** Phase 2 Core Refactoring (Issue #988)

---

## Summary

Extract agent management from the monolithic `NexusFS` class into a dedicated `AgentService` following the Gateway pattern established in the Mount and Skill service refactoring. This RFC covers:

1. **Service Extraction**: Move ~750 lines from `nexus_fs.py` into a dedicated `AgentService`
2. **Single Source of Truth**: Use `config.yaml` for all agent metadata; `EntityRegistry` only stores agent→user relationship for permission inheritance
3. **Agent Capabilities**: Define how agents declare and access role prompts, skills, and resources through a declarative config.yaml that syncs to ReBAC

---

## Motivation

### Current Problems

1. **Monolithic Implementation**: ~750 lines of agent code embedded in `nexus_fs.py` (lines 3547-4415)

2. **Dual Data Storage**: Agent metadata stored in two places:
   - `EntityRegistry.entity_metadata` (JSON in DB)
   - `config.yaml` (YAML file)

   This creates synchronization risk and update complexity.

3. **Scattered Code**: Agent logic spread across 3 files:
   - `nexus_fs.py` (~750 lines) - main implementation
   - `agents.py` (194 lines) - duplicate registration logic
   - `agent_provisioning.py` (311 lines) - thin wrappers

4. **Bloated Methods**: `get_agent()` is 190 lines due to merging data from multiple sources

5. **Inconsistent with Refactored Services**: Mount and Skill services follow the Gateway pattern; Agent does not

### Goals

- Single source of truth: `config.yaml` for all agent metadata
- EntityRegistry for relationship only (agent→user for permission inheritance)
- Clean service extraction following Gateway pattern
- Reduce code complexity by ~60%
- Maintain backward compatibility for RPC API

---

## Current Architecture

### Data Flow

```
register_agent()
    ├── EntityRegistry.register_entity()  ──→ stores name, description (DUPLICATE)
    ├── _create_agent_config_data()       ──→ builds config dict
    ├── _write_agent_config()             ──→ writes config.yaml (SOURCE OF TRUTH)
    └── rebac_create()                    ──→ grants permissions
```

### EntityRegistry Schema (Current)

```python
EntityRegistryModel:
    entity_type: str      # "agent"
    entity_id: str        # "alice,DataAnalyst"
    parent_type: str      # "user"
    parent_id: str        # "alice"
    entity_metadata: str  # '{"name": "Data Analyst", "description": "..."}' ← REMOVE
    created_at: datetime
```

### config.yaml Schema (Current)

```yaml
agent_id: alice,DataAnalyst
name: Data Analyst
user_id: alice
description: Analyzes data for insights
created_at: 2024-01-05T10:30:00Z
metadata:
  platform: langgraph
  endpoint_url: http://localhost:2024
  agent_id: agent
api_key: sk-...  # optional
```

---

## Agent Capabilities Design

Agents need three types of capabilities:

| Capability | Purpose | Storage |
|------------|---------|---------|
| **Role Prompt** | System prompt / persona | config.yaml |
| **Skill Access** | Which skills agent can use | config.yaml (declares) → ReBAC (enforces) |
| **Resource Access** | Which resources agent can read/write | config.yaml (declares) → ReBAC (enforces) |

### Design Principle

**config.yaml declares intent, ReBAC enforces access.**

- config.yaml is the declarative source: "this agent should have these capabilities"
- On registration/update, capabilities are synced to ReBAC tuples
- At runtime, ReBAC enforces all permission checks

### config.yaml Schema (Proposed)

```yaml
# === Identity ===
agent_id: alice,DataAnalyst
name: Data Analyst
user_id: alice
description: Analyzes data for insights
created_at: 2024-01-05T10:30:00Z

# === Runtime Configuration ===
metadata:
  platform: langgraph
  endpoint_url: http://localhost:2024
  agent_id: agent

# === Role Prompt (System Prompt) ===
role_prompt: |
  You are a data analyst specializing in business intelligence.
  You help users understand their data through visualization and analysis.

  Guidelines:
  - Always explain your methodology
  - Cite data sources when making claims
  - Suggest follow-up analyses when appropriate

# === Declared Capabilities ===
# These are synced to ReBAC on register/update
capabilities:
  # Skills this agent can use (relative to user's skill folder)
  skills:
    - name: data-viz
      relation: viewer        # Can use the skill
    - name: query-builder
      relation: viewer
    - name: report-generator
      relation: editor        # Can also modify this skill

  # Resources this agent can access (relative to user base path)
  resources:
    - path: /resource/datasets
      relation: viewer        # Read-only
    - path: /resource/reports
      relation: editor        # Read-write
    - path: /workspace/analysis
      relation: editor

# === Optional: API Key ===
api_key: sk-...  # Only if generate_api_key=True
```

### Capability Sync to ReBAC

On `register_agent()` or `update_agent()`, the service syncs capabilities to ReBAC:

```python
def _sync_capabilities(
    self,
    agent_id: str,
    user_id: str,
    tenant_id: str,
    capabilities: dict,
) -> None:
    """Sync declared capabilities to ReBAC tuples."""
    user_base = f"/tenant:{tenant_id}/user:{user_id}"

    # Sync skill access
    for skill in capabilities.get("skills", []):
        skill_path = f"{user_base}/skill/{skill['name']}"
        self._gw.rebac_create(
            subject=("agent", agent_id),
            relation=skill.get("relation", "viewer"),
            object=("file", skill_path),
            tenant_id=tenant_id,
        )

    # Sync resource access
    for resource in capabilities.get("resources", []):
        resource_path = f"{user_base}{resource['path']}"
        self._gw.rebac_create(
            subject=("agent", agent_id),
            relation=resource.get("relation", "viewer"),
            object=("file", resource_path),
            tenant_id=tenant_id,
        )
```

### Capability Revocation on Update

When capabilities are removed from config.yaml, we need to revoke ReBAC tuples:

```python
def _update_capabilities(
    self,
    agent_id: str,
    old_capabilities: dict,
    new_capabilities: dict,
    ...
) -> None:
    """Update capabilities: add new, remove old."""
    old_skills = {s["name"] for s in old_capabilities.get("skills", [])}
    new_skills = {s["name"] for s in new_capabilities.get("skills", [])}

    # Revoke removed skills
    for skill_name in old_skills - new_skills:
        self._gw.rebac_delete(
            subject=("agent", agent_id),
            object=("file", f"{user_base}/skill/{skill_name}"),
            tenant_id=tenant_id,
        )

    # Grant new skills
    for skill_name in new_skills - old_skills:
        # ... create tuple
```

### Manifesting Capabilities at Runtime

New method to load everything an agent needs:

```python
def get_context(
    self,
    agent_id: str,
    user_id: str,
    tenant_id: str,
) -> AgentContext:
    """Load agent's full runtime context.

    Returns everything an agent needs to start:
    - Role prompt for system message
    - Skill prompts for tool descriptions
    - List of accessible resources
    """
    config = self._read_config(agent_id, user_id, tenant_id)

    # Load skill prompt contexts
    skill_contexts = []
    for skill in config.get("capabilities", {}).get("skills", []):
        skill_path = f"/tenant:{tenant_id}/user:{user_id}/skill/{skill['name']}"
        try:
            ctx = self._skill_service.get_prompt_context(skill_path)
            skill_contexts.append(ctx)
        except PermissionError:
            pass  # Skip inaccessible skills

    return AgentContext(
        agent_id=agent_id,
        name=config.get("name"),
        role_prompt=config.get("role_prompt"),
        skills=skill_contexts,
        resources=config.get("capabilities", {}).get("resources", []),
        metadata=config.get("metadata", {}),
    )
```

### AgentContext Dataclass

```python
@dataclass
class AgentContext:
    """Runtime context for an agent."""
    agent_id: str
    name: str
    role_prompt: str | None
    skills: list[SkillPromptContext]  # From skill_service.get_prompt_context()
    resources: list[dict]              # Declared resource access
    metadata: dict                     # Platform config (endpoint_url, etc.)

    def build_system_prompt(self) -> str:
        """Build complete system prompt with role and skill descriptions."""
        parts = []

        if self.role_prompt:
            parts.append(self.role_prompt)

        if self.skills:
            parts.append("\n## Available Skills\n")
            for skill in self.skills:
                parts.append(f"### {skill.name}\n{skill.description}\n")

        return "\n".join(parts)
```

### RPC API Addition

New endpoint to get agent runtime context:

```python
@rpc_expose(description="Get agent runtime context")
def get_agent_context(
    self,
    agent_id: str,
    context: dict | None = None,
) -> dict:
    """Get agent's full runtime context (v0.6.0).

    Returns:
        AgentContext as dict with role_prompt, skills, resources
    """
    user_id = self._extract_user_id(context)
    tenant_id = self._extract_tenant_id(context) or "default"

    return asdict(self._agent_service.get_context(
        agent_id=agent_id,
        user_id=user_id,
        tenant_id=tenant_id,
    ))
```

---

## Proposed Design

### Architecture Overview

```
NexusFS
    └── @cached_property _agent_service → AgentService
                                              │
                                              ├── _gw: NexusFSGateway
                                              ├── _entity_registry: EntityRegistry
                                              └── _api_key_auth: DatabaseAPIKeyAuth
```

### New File Structure

```
src/nexus/services/
    ├── gateway.py              # Existing
    ├── mount_service.py        # Existing
    ├── skill_service.py        # Existing
    └── agent_service.py        # NEW (~300 lines)

src/nexus/core/
    ├── agents.py               # DELETE (merge into service)
    ├── agent_provisioning.py   # KEEP (convenience functions)
    └── nexus_fs.py             # REDUCE (thin delegation)
```

### AgentService Class

```python
# src/nexus/services/agent_service.py

class AgentService:
    """Agent management service.

    Uses config.yaml as single source of truth for agent data.
    EntityRegistry stores only agent→user relationship for permission inheritance.
    """

    def __init__(
        self,
        gateway: NexusFSGateway,
        entity_registry: EntityRegistry,
        session_factory: Callable[[], Session],
    ) -> None:
        self._gw = gateway
        self._entity_registry = entity_registry
        self._session_factory = session_factory

    # ===== Public API (RPC exposed via NexusFS) =====

    def register(
        self,
        agent_id: str,
        name: str,
        user_id: str,
        tenant_id: str,
        description: str | None = None,
        metadata: dict | None = None,
        generate_api_key: bool = False,
    ) -> dict:
        """Register a new agent."""
        ...

    def update(
        self,
        agent_id: str,
        user_id: str,
        tenant_id: str,
        name: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Update agent configuration."""
        ...

    def get(
        self,
        agent_id: str,
        user_id: str,
        tenant_id: str,
    ) -> dict | None:
        """Get agent details from config.yaml."""
        ...

    def list(
        self,
        user_id: str | None = None,
        tenant_id: str = "default",
    ) -> list[dict]:
        """List agents by globbing config.yaml files."""
        ...

    def delete(
        self,
        agent_id: str,
        user_id: str,
        tenant_id: str,
    ) -> bool:
        """Delete agent and cleanup resources."""
        ...

    # ===== Internal Methods =====

    def _get_agent_dir(self, agent_id: str, user_id: str, tenant_id: str) -> str:
        """Get agent directory path."""
        agent_name = agent_id.split(",", 1)[1] if "," in agent_id else agent_id
        return f"/tenant:{tenant_id}/user:{user_id}/agent/{agent_name}"

    def _read_config(self, config_path: str) -> dict | None:
        """Read and parse config.yaml."""
        ...

    def _write_config(self, config_path: str, config: dict) -> None:
        """Write config.yaml."""
        ...

    def _create_api_key(
        self,
        agent_id: str,
        user_id: str,
        tenant_id: str,
    ) -> str:
        """Generate API key for agent."""
        ...

    # ===== Capabilities Methods =====

    def get_context(
        self,
        agent_id: str,
        user_id: str,
        tenant_id: str,
    ) -> AgentContext:
        """Load agent's full runtime context."""
        ...

    def _sync_capabilities(
        self,
        agent_id: str,
        user_id: str,
        tenant_id: str,
        capabilities: dict,
    ) -> None:
        """Sync declared capabilities to ReBAC tuples."""
        ...

    def _revoke_capabilities(
        self,
        agent_id: str,
        user_id: str,
        tenant_id: str,
    ) -> None:
        """Revoke all capability ReBAC tuples for agent."""
        ...
```

### EntityRegistry Changes

**Before:**
```python
entity_registry.register_entity(
    entity_type="agent",
    entity_id=agent_id,
    parent_type="user",
    parent_id=user_id,
    entity_metadata={"name": name, "description": desc},  # ← REMOVE
)
```

**After:**
```python
entity_registry.register_entity(
    entity_type="agent",
    entity_id=agent_id,
    parent_type="user",
    parent_id=user_id,
    entity_metadata=None,  # No metadata - config.yaml is source of truth
)
```

### NexusFS Thin Delegation

```python
# In nexus_fs.py

@cached_property
def _agent_service(self) -> AgentService:
    from nexus.services.agent_service import AgentService
    return AgentService(
        gateway=self._gateway,
        entity_registry=self._entity_registry,
        session_factory=self.metadata.SessionLocal,
    )

@rpc_expose(description="Register an AI agent")
def register_agent(
    self,
    agent_id: str,
    name: str,
    description: str | None = None,
    generate_api_key: bool = False,
    metadata: dict | None = None,
    context: dict | None = None,
) -> dict:
    """Register an AI agent (v0.5.0)."""
    user_id = self._extract_user_id(context)
    tenant_id = self._extract_tenant_id(context) or "default"

    return self._agent_service.register(
        agent_id=agent_id,
        name=name,
        user_id=user_id,
        tenant_id=tenant_id,
        description=description,
        metadata=metadata,
        generate_api_key=generate_api_key,
    )
```

---

## Data Migration

### list_agents() Change

**Before:** Query EntityRegistry, then read each config.yaml for metadata

**After:** Glob `*/agent/*/config.yaml`, parse each file

```python
def list(self, user_id: str | None = None, tenant_id: str = "default") -> list[dict]:
    """List agents by globbing config.yaml files."""
    if user_id:
        pattern = f"/tenant:{tenant_id}/user:{user_id}/agent/*/config.yaml"
    else:
        pattern = f"/tenant:{tenant_id}/user:*/agent/*/config.yaml"

    config_paths = self._gw.glob(pattern)
    agents = []

    for path in config_paths:
        config = self._read_config(path)
        if config:
            agents.append(self._config_to_agent_info(config, path))

    return agents
```

### Permission Inheritance

No changes needed. Permission system continues to use:

```python
parent = entity_registry.get_parent("agent", agent_id)
# Returns user entity for permission inheritance check
```

This works because we keep EntityRegistry for the relationship, just remove the metadata.

---

## Files to Modify

| File | Action | Changes |
|------|--------|---------|
| `src/nexus/services/agent_service.py` | CREATE | New service (~300 lines) |
| `src/nexus/core/nexus_fs.py` | MODIFY | Remove ~700 lines, add thin delegation (~50 lines) |
| `src/nexus/core/agents.py` | DELETE | Merge into AgentService |
| `src/nexus/core/agent_provisioning.py` | KEEP | Update to use AgentService |
| `src/nexus/core/entity_registry.py` | KEEP | No changes needed |
| `src/nexus/services/gateway.py` | MODIFY | Add glob() method if not present |

### Lines of Code Impact

| Before | After | Reduction |
|--------|-------|-----------|
| nexus_fs.py: ~750 lines (agent) | ~50 lines (delegation) | -700 |
| agents.py: 194 lines | 0 (deleted) | -194 |
| agent_service.py: 0 | ~300 lines | +300 |
| **Total** | | **~594 lines removed** |

---

## API Compatibility

### RPC API

Existing methods remain unchanged, with one new addition:

```python
# Existing methods (unchanged signatures)
register_agent(agent_id, name, description, generate_api_key, metadata, context)
update_agent(agent_id, name, description, metadata, context)
list_agents(context)
get_agent(agent_id, context)
delete_agent(agent_id, context)

# New method (v0.6.0)
get_agent_context(agent_id, context)  # Returns AgentContext with role_prompt, skills, resources
```

### register_agent Enhancement

`register_agent` gains new optional parameters:

```python
register_agent(
    agent_id: str,
    name: str,
    description: str | None = None,
    generate_api_key: bool = False,
    metadata: dict | None = None,
    # New in v0.6.0:
    role_prompt: str | None = None,
    capabilities: dict | None = None,  # {"skills": [...], "resources": [...]}
    context: dict | None = None,
) -> dict
```

### Internal API Changes

`agents.py` functions will be removed. Any code importing from `nexus.core.agents` needs to be updated:

```python
# Before
from nexus.core.agents import register_agent, validate_agent_ownership

# After
# Use AgentService directly or via NexusFS
nx.register_agent(...)
```

---

## Testing Strategy

1. **Unit Tests**: Test AgentService methods in isolation with mocked Gateway
2. **Integration Tests**: Verify RPC API compatibility
3. **Migration Test**: Ensure existing agents are readable after refactor
4. **Permission Tests**: Verify agent→user inheritance still works

### Key Test Cases

```python
# === Core CRUD Tests ===

def test_register_agent_creates_config_yaml():
    """config.yaml should be created with all metadata."""

def test_register_agent_creates_entity_registry_without_metadata():
    """EntityRegistry should only store relationship, not metadata."""

def test_get_agent_reads_from_config_yaml():
    """get() should read from config.yaml, not EntityRegistry."""

def test_list_agents_uses_glob():
    """list() should glob config.yaml files, not query EntityRegistry."""

def test_permission_inheritance_still_works():
    """Agent should inherit permissions from owner user."""

# === Capability Tests ===

def test_register_with_capabilities_creates_rebac_tuples():
    """Declared capabilities should create ReBAC tuples."""
    agent = register_agent(
        agent_id="alice,Analyst",
        capabilities={
            "skills": [{"name": "data-viz", "relation": "viewer"}],
            "resources": [{"path": "/resource/data", "relation": "viewer"}],
        }
    )
    # Verify ReBAC tuples exist
    assert rebac_check(("agent", "alice,Analyst"), "viewer", skill_path)
    assert rebac_check(("agent", "alice,Analyst"), "viewer", resource_path)

def test_update_capabilities_revokes_removed_skills():
    """Removing a skill from capabilities should revoke ReBAC tuple."""

def test_get_context_returns_role_prompt_and_skills():
    """get_context() should return full runtime context."""
    ctx = get_agent_context("alice,Analyst")
    assert ctx["role_prompt"] == "You are a data analyst..."
    assert len(ctx["skills"]) == 2

def test_get_context_loads_skill_prompts():
    """get_context() should load SkillPromptContext for each skill."""

def test_delete_agent_revokes_all_capabilities():
    """Deleting agent should revoke all capability ReBAC tuples."""
```

---

## Rollout Plan

### Phase 1: Create AgentService (Non-breaking)
1. Create `agent_service.py` with new implementation
2. Add tests for new service
3. Keep existing code in `nexus_fs.py` unchanged

### Phase 2: Wire Up Delegation
1. Add `_agent_service` property to NexusFS
2. Update RPC methods to delegate to service
3. Run integration tests

### Phase 3: Cleanup
1. Delete `agents.py`
2. Remove old implementation from `nexus_fs.py`
3. Update `agent_provisioning.py` if needed

### Phase 4: Data Cleanup (Optional)
1. Migration script to clear `entity_metadata` for existing agents
2. Keep relationship data intact

---

## Open Questions

1. **Caching**: Should we cache parsed config.yaml in memory for list_agents()?
   - Pro: Faster repeated calls
   - Con: Cache invalidation complexity

2. **Pagination**: Should list_agents() support pagination?
   - Current: Returns all agents
   - Proposed: Add `limit` and `offset` parameters

3. **agent_provisioning.py**: Keep as separate file or merge into AgentService?
   - Recommendation: Keep separate - it's user-facing convenience functions

---

## References

- [Mount Service Refactoring PR #1011](https://github.com/nexi-lab/nexus/pull/1011)
- [Phase 2 Core Refactoring Issue #988](https://github.com/nexi-lab/nexus/issues/988)
- [Gateway Pattern in gateway.py](../src/nexus/services/gateway.py)
