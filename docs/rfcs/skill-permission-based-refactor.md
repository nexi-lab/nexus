# RFC: Skill Permission-Based Refactoring

## Summary

Refactor the skill module to use ReBAC permissions as the unified mechanism for both **distribution** (sharing/publishing) and **runner** (agent access) concerns.

## Background

### Current Skill Framework

Skills have three parts:
- **Builder** - Creating/authoring skills (✅ complete, no changes needed)
- **Distributor** - Publishing/sharing/discovering skills
- **Runner** - Using/executing skills by AI agents

### Problems with Current Approach

**Distribution (Copy-Based):**
- Publishing copies skills to different tier paths
- Multiple copies create sync issues
- Complex governance (which copy is canonical?)
- Storage duplication

**Runner:**
- No optimized APIs for agent system prompt injection
- No progressive disclosure (metadata vs full content)
- No permission-based filtering

## Proposed Solution: Unified Permission Model

### Core Principle

**Skills have one canonical location. Visibility is controlled by permissions.**

### Path Structure: Tenant/User-First

We use **tenant/user-first** paths (not resource-first):

```
# ✅ Tenant/User-First (chosen)
/tenant:<tenant_id>/user:<user_id>/skill/<skill_name>/
/tenant:<tenant_id>/user:<user_id>/agents/<agent_id>/

# ❌ Resource-First (rejected)
/skill/<tenant>/<user>/<skill_name>/
/agents/<tenant>/<user>/<agent_id>/
```

**Why tenant/user-first:**
- All user resources in one place (like Unix home dirs)
- Natural permission inheritance: grant access to `/tenant:acme/user:alice/` → inherits to all resources
- Easy tenant isolation: `/tenant:acme/` contains everything for tenant
- User-centric operations: delete user = delete `/tenant:acme/user:alice/`

**Full namespace structure:**
```
/tenant:acme/                              # Tenant
  user:alice/                              # User namespace
    skill/
      code-review/                         # Alice's skill
      testing/
    agents/
      code-assistant/
  user:bob/
    skill/
      code-review/                         # Bob's skill (no collision)
  .system/                                 # Tenant-wide system resources
    skill/
      default-review/
/skill/                                    # Global system skills
  builtin-helpers/
```

### Skill Identity

**The full path is the unique identifier, not just the skill name.**

```
/tenant:acme/user:alice/skill/code-review/  ← Alice's code-review skill
/tenant:acme/user:bob/skill/code-review/    ← Bob's code-review skill (different skill!)
```

This means:
- Two users can create skills with the same name (no collision)
- Agent configs reference skills by full path (unambiguous)
- Display shows owner info to distinguish same-named skills

```
Skill location: /tenant:acme/user:alice/skill/my-skill/
                    (single source of truth)

Visibility = Permissions:
┌─────────────────────────────────────────────────────────────┐
│  Private (default)                                          │
│  → Only creator has access                                  │
├─────────────────────────────────────────────────────────────┤
│  Shared with user/agent                                     │
│  → rebac_create(user:bob, viewer, skill_path)               │
│  → rebac_create(agent:code-bot, viewer, skill_path)         │
├─────────────────────────────────────────────────────────────┤
│  Shared with tenant                                         │
│  → rebac_create(tenant:acme, viewer, skill_path)            │
├─────────────────────────────────────────────────────────────┤
│  Public (system-wide)                                       │
│  → rebac_create(role:public, viewer, skill_path)            │
└─────────────────────────────────────────────────────────────┘
```

## User Journeys

### 1. Builder (Conversational)

Skill building is **conversational**, not a static API call. The agent uses the `skill-creator` system skill to guide the process.

**Simple Case (1 interaction):**
```
User: "Create a skill for reviewing Python security"
    ↓
Agent: [drafts complete SKILL.md, saves]
    ↓
"Created 'python-security-review' at /tenant:acme/user:alice/skill/python-security-review/. Ready to use!"
```

**Complex Case (multiple interactions):**
```
User: "Create a skill for code review"
    ↓
Agent: "What aspects? Security, performance, style, or all?"
    ↓
User: "Security focused, with OWASP top 10"
    ↓
Agent: [drafts] "Here's a preview. Want changes?"
    ↓
User: "Add examples for each vulnerability"
    ↓
Agent: [updates, saves] "Created with examples. Ready to use!"
```

**Why conversational:**
- Agent can clarify vague requests
- User can preview before saving
- Natural refinement loop
- Still fast for simple cases (1 turn)

**Filesystem-native approach:**

Skills are just files. The agent uses standard filesystem operations:

```python
# Agent writes SKILL.md directly using filesystem
write("/tenant:<tenant_id>/user:<user_id>/skill/<name>/SKILL.md", content)

# Skill is private by default (only creator has access)
# No special API needed - permissions auto-granted to creator
```

**No special create API needed** because:
- Conversational flow means agent builds content iteratively
- Agent already has filesystem access via `write()`
- Permissions are granted automatically to file creator
- Keeps skill operations filesystem-native

**System skill:** `/skill/skill-creator/` guides agents on the conversational flow.

### 2. Distributor (Permission-Based)

**Old Flow (Copy-Based):**
```
skills_publish(skill, target="tenant")
    → Copies to /tenant:acme/.system/skill/my-skill/
```

**New Flow (Permission-Based):**
```
skills_share(skill, share_with="tenant")
    → rebac_create(tenant:acme, viewer, skill_path)
    → No copy, just permission grant
```

**Sharing Examples:**
```python
skill_path = "/tenant:acme/user:alice/skill/my-skill"

# Share publicly (all users globally)
skills_share(skill_path, "public", context)
    → rebac_write(("role", "public"), "viewer", skill_path)
    # Uses role:public - already implemented in the system

# Share with a group (all members of the group)
skills_share(skill_path, "group:engineering", context)
    → rebac_write(("group", "engineering", "member"), "viewer", skill_path)
    # Uses 3-tuple (userset-as-subject): all members of the group get access

# Share with specific user
skills_share(skill_path, "user:bob@example.com", context)
    → rebac_write(("user", "bob@example.com"), "viewer", skill_path)

# Share with specific agent
skills_share(skill_path, "agent:code-assistant", context)
    → rebac_write(("agent", "code-assistant"), "viewer", skill_path)
```

**Revoking Access:**
```python
skills_unshare("/tenant:acme/user:alice/skill/my-skill", "user:bob@example.com", context)
    → rebac_delete(user:bob, viewer, skill_path)
```

**Important:** Sharing controls *visibility*, not *usage*. Users must also *subscribe* to a skill to add it to their library.

### 3. Subscribing to Skills

Sharing makes a skill visible. Subscribing adds it to the user's library.

```
┌─────────────────────────────────────────────────────────────┐
│  Visibility (permissions)     │  User Library (subscriptions) │
│───────────────────────────────│───────────────────────────────│
│  skills_share()               │  skills_subscribe()           │
│  skills_unshare()             │  skills_unsubscribe()         │
│                               │  skills_discover() - browse   │
└─────────────────────────────────────────────────────────────┘
```

**Discovery & Subscription Flow:**
```python
# Bob discovers available skills (ones he has permission to see)
skills_discover(context, filter="public")
    → Returns: [{"path": "/tenant:acme/user:alice/skill/code-review", ...}, ...]

# Bob subscribes to a skill (adds to his library)
skills_subscribe("/tenant:acme/user:alice/skill/code-review", context)
    → Adds to Bob's subscribed_skills config

# Bob unsubscribes from a skill
skills_unsubscribe("/tenant:acme/user:alice/skill/code-review", context)
    → Removes from subscribed_skills
```

**Note:** Creator's own skills are auto-subscribed on creation.

### 4. Agent Skill Assignment

Agent-level skill assignment is handled by the agent's own config, not via skill APIs.

```yaml
# Agent config: /tenant:<tenant_id>/user:<user_id>/agents/<agent_id>/config.yaml
active_skills:
  - "/tenant:acme/user:alice/skill/code-review"    # Must be in user's subscriptions
  - "/tenant:acme/user:bob/skill/testing"
```

**Why separate from skill APIs:**
- Agent config is managed by the agent system, not skill system
- Different agents can use different subsets of subscribed skills
- Keeps skill APIs focused on discovery and access control

### 5. Runner (Agent Using Skills)

**Phase 1: System Prompt Injection**
```
Agent session starts
    ↓
skills_get_prompt_context(context)
    ↓
Returns skills user has subscribed to AND has read permission on
    ↓
Agent filters based on its own active_skills config
    ↓
Metadata injected into system prompt (~100 tokens/skill)
```

**Phase 2: On-Demand Loading**
```
Agent decides skill is relevant
    ↓
skills_load("code-review", context)
    ↓
Permission check: rebac_check(agent, read, skill_path)
    ↓
Returns full SKILL.md content (<5000 tokens)
```

**Phase 3: Execution**
```
Agent follows SKILL.md instructions
    ↓
Runs scripts via bash (if permitted)
    ↓
Reads references via filesystem
```

## API Changes

### Filesystem Operations (Existing)

Skills use standard filesystem operations for CRUD - no special skill APIs needed:

```python
# Create/Update skill
write("/tenant:<tid>/user:<uid>/skill/<name>/SKILL.md", content)

# Read skill
read("/tenant:<tid>/user:<uid>/skill/<name>/SKILL.md")

# Delete skill
delete("/tenant:<tid>/user:<uid>/skill/<name>/")

# List skills
list("/tenant:<tid>/user:<uid>/skill/")
```

### New Skill-Specific APIs

These APIs handle operations that go beyond basic filesystem CRUD:

```python
@rpc_expose
def skills_share(
    skill_path: str,  # Full path like "/tenant:acme/user:alice/skill/code-review"
    share_with: str,  # "tenant" | "public" | "group:X" | "user:X" | "agent:X"
    context: OperationContext,
) -> dict:
    """Share skill by granting read permission.

    Args:
        skill_path: Full path to the skill
        share_with: Target to share with:
            - "tenant" - all users in current tenant
            - "public" - everyone (role:public)
            - "group:engineering" - all members of a group
            - "user:bob@example.com" - specific user
            - "agent:code-assistant" - specific agent
        context: Operation context (must be skill owner)

    Returns:
        {"success": True, "shared_with": "tenant:acme"}
    """

@rpc_expose
def skills_unshare(
    skill_path: str,
    unshare_from: str,
    context: OperationContext,
) -> dict:
    """Revoke skill access by removing permission."""

@rpc_expose
def skills_discover(
    context: OperationContext,
    filter: str = "all",  # "all" | "public" | "tenant" | "subscribed"
) -> dict:
    """Discover skills user has permission to access.

    Returns:
        {
            "skills": [
                {
                    "name": "code-review",
                    "owner": "alice",
                    "path": "/tenant:acme/user:alice/skill/code-review",
                    "description": "...",
                    "is_subscribed": True
                }
            ],
            "count": 25
        }
    """

@rpc_expose
def skills_subscribe(
    skill_path: str,
    context: OperationContext,
) -> dict:
    """Subscribe to a skill (add to user's library).

    Adds skill to user's subscribed_skills config.
    Requires read permission on the skill.
    """

@rpc_expose
def skills_unsubscribe(
    skill_path: str,
    context: OperationContext,
) -> dict:
    """Unsubscribe from a skill (remove from user's library)."""

@rpc_expose
def skills_get_prompt_context(
    context: OperationContext,
    max_skills: int = 50,
) -> dict:
    """Get skills metadata for system prompt injection.

    Returns skills the user has subscribed to AND has read permission on.
    Agent can further filter based on its own active_skills config.
    Optimized for low token count (~100 tokens/skill).

    Returns:
        {
            "xml": "<available_skills>...</available_skills>",
            "skills": [
                {
                    "name": "code-review",
                    "owner": "alice",           # Owner for disambiguation
                    "description": "...",
                    "path": "/tenant:acme/user:alice/skill/code-review"
                }
            ],
            "count": 12,
            "token_estimate": 1200
        }
    """

@rpc_expose
def skills_load(
    skill_path: str,  # Full path, not just name (avoids ambiguity)
    context: OperationContext,
) -> dict:
    """Load full skill content on-demand.

    Args:
        skill_path: Full path like "/tenant:acme/user:alice/skill/code-review"

    Checks read permission before returning content.

    Returns:
        {
            "name": "code-review",
            "owner": "alice",
            "path": "/tenant:acme/user:alice/skill/code-review",
            "metadata": {...},
            "content": "# Full SKILL.md markdown...",
            "scripts": ["/path/to/script.py"],
            "references": ["/path/to/guide.md"]
        }

    Raises:
        PermissionError: If caller lacks read permission
    """
```

### Deprecated APIs

| Old API | Replacement |
|---------|-------------|
| `skills_publish(skill, target)` | `skills_share(skill, target)` |
| `skills_create()` | Filesystem `write()` |
| `skills_create_from_content()` | Filesystem `write()` |
| `skills_list()` | `skills_discover()` |
| `skills_info()` | `skills_load()` |
| `skills_fork()` | Filesystem `read()` + `write()` |
| `skills_submit_approval()` | Skipped (governance out of scope) |
| `skills_approve()` | Skipped (governance out of scope) |
| `skills_reject()` | Skipped (governance out of scope) |

### Governance (Out of Scope)

Governance APIs are out of scope for this refactoring. If needed, they can be added as a separate feature using the ReBAC permission system.

## Benefits

| Aspect | Copy-Based (Old) | Permission-Based (New) |
|--------|------------------|------------------------|
| Single source of truth | ❌ Multiple copies | ✅ One location |
| Updates propagate | ❌ Manual sync | ✅ Automatic |
| Storage | ❌ Duplicated | ✅ Efficient |
| Revoke access | ❌ Delete copies | ✅ Remove permission |
| Audit trail | ❌ Complex | ✅ Permission history |
| Fine-grained sharing | ❌ Tier-only | ✅ Any subject |
| API surface | ❌ Skill-specific CRUD | ✅ Filesystem-native |
| Draft handling | ❌ Separate state | ✅ Private = draft |

## Architecture: Mixin + Service + Cache

### Design Pattern

We use a **Mixin + Service + optional Cache** pattern instead of a Registry:

```
┌───────────────────────────────────────────────────────────────┐
│  SkillsMixin                                                  │
│    @rpc_expose methods (thin delegation)                      │
└──────────────────────────┬────────────────────────────────────┘
                           │
┌──────────────────────────▼────────────────────────────────────┐
│  SkillService (stateless business logic)                      │
│    - share/unshare (ReBAC writes)                             │
│    - discover (ReBAC query + metadata fetch)                  │
│    - subscribe/unsubscribe (user config read/write)           │
│    - load (permission check + file read)                      │
│    - get_prompt_context (aggregate subscribed + permitted)    │
└───────────┬───────────────────┬───────────────────┬───────────┘
            │                   │                   │
            ▼                   ▼                   ▼
     ┌────────────┐      ┌────────────┐      ┌────────────┐
     │  ReBAC     │      │ Filesystem │      │ UserConfig │
     │ (who sees) │      │ (content)  │      │ (library)  │
     └────────────┘      └────────────┘      └────────────┘

     ┌────────────┐
     │ SkillCache │  (optional, for performance)
     └────────────┘
```

### Why No Registry?

With permission-based visibility, **we don't need a "Registry"**:

| Old Model (Registry) | New Model (Service) |
|---------------------|---------------------|
| Discovery = scan filesystem paths | Discovery = query ReBAC permissions |
| Register skills in memory | Skills are just files |
| Registry holds state | Service is stateless |
| Cache invalidation complex | Cache is optional/simple |

The "registry" concept is replaced by ReBAC queries for discovery.

### Component Responsibilities

| Component | Responsibility | State |
|-----------|---------------|-------|
| **SkillsMixin** | RPC method definitions, validation | None (delegates) |
| **SkillService** | Business logic orchestration | Stateless |
| **SkillCache** | Performance optimization (optional) | LRU cache |
| **ReBAC** | Permission storage & queries | Permissions DB |
| **Filesystem** | Skill content storage | Files |
| **UserConfig** | User subscription lists | Per-user config |

### SkillService Interface

```python
class SkillService:
    """Stateless skill operations. All state lives in ReBAC/FS/Config."""

    def __init__(
        self,
        filesystem: NexusFilesystem,
        rebac: ReBACManager,
        user_config: UserConfigStore,
        cache: SkillCache | None = None,
    ): ...

    # Distribution
    def share(self, skill_path: str, share_with: str, context: OperationContext) -> None: ...
    def unshare(self, skill_path: str, unshare_from: str, context: OperationContext) -> None: ...

    # Discovery & Subscription
    def discover(self, context: OperationContext, filter: str = "all") -> list[SkillInfo]: ...
    def subscribe(self, skill_path: str, context: OperationContext) -> None: ...
    def unsubscribe(self, skill_path: str, context: OperationContext) -> None: ...

    # Runner
    def get_prompt_context(self, context: OperationContext, max_skills: int = 50) -> PromptContext: ...
    def load(self, skill_path: str, context: OperationContext) -> SkillContent: ...
```

### SkillCache Interface

```python
class SkillCache:
    """Optional LRU cache for skill metadata. Not a registry - just performance."""

    def __init__(self, max_size: int = 1000, ttl_seconds: int = 300): ...

    def get_metadata(self, path: str) -> SkillMetadata | None: ...
    def set_metadata(self, path: str, metadata: SkillMetadata) -> None: ...
    def invalidate(self, path: str) -> None: ...
    def clear(self) -> None: ...
```

### SkillsMixin Pattern

```python
class SkillsMixin:
    """RPC methods for skills. Thin layer that delegates to SkillService."""

    _skill_service: SkillService  # Lazily created or injected

    @rpc_expose
    def skills_share(self, skill_path: str, share_with: str, context: OperationContext) -> dict:
        self._skill_service.share(skill_path, share_with, context)
        return {"success": True, "shared_with": share_with}

    @rpc_expose
    def skills_discover(self, context: OperationContext, filter: str = "all") -> dict:
        skills = self._skill_service.discover(context, filter)
        return {"skills": [s.to_dict() for s in skills], "count": len(skills)}

    # ... other @rpc_expose methods delegate similarly
```

## Implementation Plan

### Phase 1: User Subscriptions Config

1. Add `_get_subscriptions_path(context)` helper
2. Add `_load_subscriptions(context)` to read user subscriptions
3. Add `_save_subscriptions(context, skills)` to write subscriptions

### Phase 2: Distribution APIs

1. Add `skills_share()` to mixin
2. Add `skills_unshare()` to mixin

### Phase 3: Subscription APIs

1. Add `skills_discover()` to mixin
2. Add `skills_subscribe()` to mixin
3. Add `skills_unsubscribe()` to mixin
4. Add permission checks to SkillRegistry discovery

### Phase 4: Runner APIs

1. Add `skills_get_prompt_context()` to mixin
2. Add `skills_load()` to mixin
3. Auto-subscribe creator's skills on creation

### Phase 5: Cleanup

1. Remove CRUD wrapper methods (skills_create, skills_list, etc.)
2. Remove governance methods
3. Remove tier-based copy logic from SkillManager
4. Update skill path conventions

## Files to Modify

### New Files
- `src/nexus/skills/service.py` - New SkillService with stateless business logic
- `src/nexus/skills/cache.py` - Optional SkillCache (LRU cache for metadata)
- `src/nexus/skills/types.py` - SkillInfo, SkillContent, PromptContext dataclasses
- `tests/unit/skills/test_skill_service.py` - Tests for SkillService
- `tests/unit/skills/test_skill_cache.py` - Tests for SkillCache

### Modified Files
- `src/nexus/core/nexus_fs_skills.py` - Update SkillsMixin to delegate to SkillService
- `src/nexus/skills/parser.py` - Keep as-is (parses SKILL.md format)
- `src/nexus/skills/models.py` - Keep as-is (Skill, SkillMetadata dataclasses)

### Deprecated/Removed Files
- `src/nexus/skills/registry.py` - Remove (replaced by ReBAC-based discovery)
- `src/nexus/skills/manager.py` - Remove (copy logic no longer needed)

## Success Criteria

- [ ] Skills created via filesystem `write()` are private by default
- [ ] Creator automatically gets owner permission on skill creation
- [ ] Creator's skills are auto-subscribed on creation
- [ ] `skills_discover()` returns skills user has permission to see with `is_subscribed` flag
- [ ] `skills_subscribe()` adds skill to user's library
- [ ] `skills_unsubscribe()` removes skill from user's library
- [ ] `skills_get_prompt_context()` returns only subscribed AND permitted skills
- [ ] `skills_load()` respects read permissions
- [ ] `skills_share()` grants correct permissions
- [ ] `skills_unshare()` revokes access correctly
- [ ] Progressive disclosure: metadata ~100 tokens, full content <5000 tokens
- [ ] All existing import/export/search tests pass
- [ ] New permission tests pass

## Design Decisions

### Naming Collisions (Resolved)

**Q: What if two users create skills with the same name?**

A: Full path is the unique identifier, not the skill name. This is similar to how two GitHub users can have repos with the same name.

- Agent configs use full paths: `/acme/alice/skills/code-review`
- API responses include `owner` field for display disambiguation
- No collision because paths are different

**Display example when user has access to both:**
```xml
<available_skills>
  <skill name="code-review" owner="alice" path="...">
    Code review guidelines for Python projects
  </skill>
  <skill name="code-review" owner="bob" path="...">
    Security-focused code review checklist
  </skill>
</available_skills>
```

### System Skills (Resolved)

**Q: Where do system/built-in skills live?**

A: Two levels of system skills:
- **Tenant-wide**: `/<tenant>/.system/skills/` - shared across all users in tenant
- **Global**: `/system/skills/` - built-in skills available to all tenants

Both use the same permission model. Global skills have `role:public` viewer permission by default.

**Required system skills:**
- `/system/skills/skill-creator/` - Guides agents on conversational skill building

### Filesystem-Native CRUD (Resolved)

**Q: Do we need a `skills_create_from_content()` API?**

A: No. Skills are files, so we use filesystem operations directly.

**Reasoning:**
- Skills are just SKILL.md files in the filesystem
- Agent already has filesystem access via `write()`, `read()`, `delete()`
- Conversational builder means agent iteratively builds content before final write
- Permissions auto-granted to file creator (private by default)
- Special APIs only needed for operations beyond CRUD: sharing, discovery, loading

**API surface:**
```
Filesystem (existing)     |  Skill-specific (new)
--------------------------|---------------------------
write()  - create/update  |  skills_share()      - grant access
read()   - read           |  skills_unshare()    - revoke access
delete() - delete         |  skills_discover()   - browse available
list()   - list           |  skills_subscribe()  - add to library
                          |  skills_unsubscribe() - remove from library
                          |  skills_get_prompt_context() - for agent
                          |  skills_load()       - formatted read
```

**Note:** Agent-level skill activation is handled by agent config, not skill APIs.

### Draft State (Resolved)

**Q: How do we handle skill drafts for testing before publishing?**

A: No special draft state needed. "Draft" = private skill, "Published" = shared skill.

- New skills are private by default (only creator has access)
- Creator can test immediately since they have access
- "Publishing" is just sharing: `skills_share(path, "tenant")`
- No separate draft folder, no TTL cleanup, no complexity

### Visibility vs Subscription (Resolved)

**Q: If a skill is shared publicly, does everyone automatically use it?**

A: No. Sharing controls *visibility*, subscription controls *user library*.

```
Share (visibility)     →  User CAN see the skill
Subscribe (library)    →  Skill is in user's library
Agent config           →  Agent WILL use specific skills from library
```

**Two-level model:**
- **User level**: `skills_subscribe()` / `skills_unsubscribe()` - manages user's library
- **Agent level**: Agent config has `active_skills` list - managed by agent system, not skill APIs

**Why separate:**
- Public skills shouldn't auto-clutter everyone's library
- Users choose which skills to subscribe to (like installing an app)
- Different agents can use different subsets of subscribed skills
- Keeps skill APIs focused on access control, not agent configuration

**Behavior:**
- Creator's own skills are auto-subscribed on creation
- Shared skills require explicit `skills_subscribe()` by recipient
- `skills_get_prompt_context()` returns skills that are both subscribed AND permitted
- Agent filters the result based on its own `active_skills` config

## Open Questions

1. **Skill discovery performance**: How to efficiently discover all skills a user has permission to read?
2. **Permission inheritance**: Should skill subdirectories (scripts/, references/) inherit parent permissions?

## References

- [Agent Skills Specification](https://agentskills.io)
- Mount permission refactoring (PR #988)
