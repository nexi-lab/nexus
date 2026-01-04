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

### 1. Builder (No Changes)

```
User creates skill
    ↓
skills_create(name, description, template)
    ↓
Skill created at: /tenant:{tid}/user:{uid}/skill/{name}/
    ↓
Creator automatically gets owner permission
```

### 2. Distributor (Permission-Based)

**Old Flow (Copy-Based):**
```
skills_publish(skill, target="tenant")
    → Copies to /tenant:acme/skill/my-skill/
```

**New Flow (Permission-Based):**
```
skills_share(skill, share_with="tenant")
    → rebac_create(tenant:acme, viewer, skill_path)
    → No copy, just permission grant
```

**Sharing Examples:**
```python
# Share with entire tenant
skills_share("my-skill", "tenant", context)

# Share publicly (all users)
skills_share("my-skill", "public", context)

# Share with specific user
skills_share("my-skill", "user:bob@example.com", context)

# Share with specific agent
skills_share("my-skill", "agent:code-assistant", context)
```

**Revoking Access:**
```python
skills_unshare("my-skill", "user:bob@example.com", context)
    → rebac_delete(user:bob, viewer, skill_path)
```

### 3. Runner (Agent Using Skills)

**Phase 1: System Prompt Injection**
```
Agent session starts
    ↓
skills_get_prompt_context(context)
    ↓
Returns only skills agent has read permission on
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

### New APIs

```python
@rpc_expose
def skills_share(
    skill_name: str,
    share_with: str,  # "tenant" | "public" | "user:X" | "agent:X"
    context: OperationContext,
) -> dict:
    """Share skill by granting read permission.

    Args:
        skill_name: Name of the skill to share
        share_with: Target to share with
        context: Operation context (must be skill owner)

    Returns:
        {"success": True, "shared_with": "tenant:acme"}
    """

@rpc_expose
def skills_unshare(
    skill_name: str,
    unshare_from: str,
    context: OperationContext,
) -> dict:
    """Revoke skill access by removing permission."""

@rpc_expose
def skills_get_prompt_context(
    context: OperationContext,
    max_skills: int = 50,
) -> dict:
    """Get skills metadata for system prompt injection.

    Returns only skills the caller has read permission on.
    Optimized for low token count (~100 tokens/skill).

    Returns:
        {
            "xml": "<available_skills>...</available_skills>",
            "skills": [
                {"name": "...", "description": "...", "location": "..."}
            ],
            "count": 12,
            "token_estimate": 1200
        }
    """

@rpc_expose
def skills_load(
    skill_name: str,
    context: OperationContext,
) -> dict:
    """Load full skill content on-demand.

    Checks read permission before returning content.

    Returns:
        {
            "name": "code-review",
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
| `skills_submit_approval()` | Keep for governance, but grants permission instead of copying |

### Governance Integration

Approval workflow remains, but the result is a permission grant:

```python
# Submit for approval (unchanged)
skills_submit_approval("my-skill", target="tenant", reviewers=["admin@acme.com"])

# Reviewer approves
skills_approve(approval_id)
    → System calls: skills_share("my-skill", "tenant", system_context)
    → Permission granted, no copy made
```

## Benefits

| Aspect | Copy-Based (Old) | Permission-Based (New) |
|--------|------------------|------------------------|
| Single source of truth | ❌ Multiple copies | ✅ One location |
| Updates propagate | ❌ Manual sync | ✅ Automatic |
| Storage | ❌ Duplicated | ✅ Efficient |
| Revoke access | ❌ Delete copies | ✅ Remove permission |
| Audit trail | ❌ Complex | ✅ Permission history |
| Fine-grained sharing | ❌ Tier-only | ✅ Any subject |

## Implementation Plan

### Phase 1: Runner APIs

1. Add `skills_get_prompt_context()` to mixin
2. Add `skills_load()` to mixin
3. Add permission checks to SkillRegistry discovery
4. Tests for permission-based filtering

### Phase 2: Distributor APIs

1. Add `skills_share()` to mixin
2. Add `skills_unshare()` to mixin
3. Update governance to use permission grants
4. Deprecate `skills_publish()` (keep for backward compat)

### Phase 3: Cleanup

1. Remove tier-based copy logic from SkillManager
2. Update skill path conventions
3. Migration script for existing multi-copy skills
4. Remove deprecated APIs in next major version

## Files to Modify

- `src/nexus/core/nexus_fs_skills.py` - Add new RPC methods
- `src/nexus/skills/registry.py` - Permission-based discovery
- `src/nexus/skills/manager.py` - Remove copy logic, add share logic
- `src/nexus/skills/governance.py` - Grant permissions on approval
- `tests/unit/skills/test_skill_permissions.py` - New tests

## Success Criteria

- [ ] `skills_get_prompt_context()` returns only permitted skills
- [ ] `skills_load()` respects read permissions
- [ ] `skills_share()` grants correct permissions
- [ ] `skills_unshare()` revokes access correctly
- [ ] Governance approval grants permission (no copy)
- [ ] Progressive disclosure: metadata ~100 tokens, full content <5000 tokens
- [ ] Backward compatible: existing APIs work during transition
- [ ] All existing skill tests pass

## Open Questions

1. **System skills**: Should system skills remain in `/skills/system/` or also use permission-based visibility?
2. **Skill discovery performance**: How to efficiently discover all skills a user has permission to read?
3. **Permission inheritance**: Should skill subdirectories (scripts/, references/) inherit parent permissions?

## References

- [Agent Skills Specification](https://agentskills.io)
- Mount permission refactoring (PR #988)
