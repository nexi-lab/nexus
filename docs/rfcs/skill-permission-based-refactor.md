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
/<tenant>/<user>/skills/<skill_name>/
/<tenant>/<user>/agents/<agent_id>/
/<tenant>/<user>/mounts/<mount_name>/

# ❌ Resource-First (rejected)
/skills/<tenant>/<user>/<skill_name>/
/agents/<tenant>/<user>/<agent_id>/
```

**Why tenant/user-first:**
- All user resources in one place (like Unix home dirs)
- Natural permission inheritance: grant access to `/acme/alice/` → inherits to all resources
- Easy tenant isolation: `/acme/` contains everything for tenant
- User-centric operations: delete user = delete `/acme/alice/`

**Full namespace structure:**
```
/acme/                              # Tenant
  alice/                            # User namespace
    skills/
      code-review/                  # Alice's skill
      testing/
    agents/
      code-assistant/
  bob/
    skills/
      code-review/                  # Bob's skill (no collision)
  .system/                          # Tenant-wide system resources
    skills/
      default-review/
/system/                            # Global system resources
  skills/
    builtin-helpers/
```

### Skill Identity

**The full path is the unique identifier, not just the skill name.**

```
/acme/alice/skills/code-review/  ← Alice's code-review skill
/acme/bob/skills/code-review/    ← Bob's code-review skill (different skill!)
```

This means:
- Two users can create skills with the same name (no collision)
- Agent configs reference skills by full path (unambiguous)
- Display shows owner info to distinguish same-named skills

```
Skill location: /acme/alice/skills/my-skill/
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
"Created 'python-security-review' at /acme/alice/skills/python-security-review/. Ready to use!"
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
write("/<tenant>/<user>/skills/<name>/SKILL.md", content)

# Skill is private by default (only creator has access)
# No special API needed - permissions auto-granted to creator
```

**No special create API needed** because:
- Conversational flow means agent builds content iteratively
- Agent already has filesystem access via `write()`
- Permissions are granted automatically to file creator
- Keeps skill operations filesystem-native

**System skill:** `/system/skills/skill-creator/` guides agents on the conversational flow.

### 2. Distributor (Permission-Based)

**Old Flow (Copy-Based):**
```
skills_publish(skill, target="tenant")
    → Copies to /acme/.system/skills/my-skill/
```

**New Flow (Permission-Based):**
```
skills_share(skill, share_with="tenant")
    → rebac_create(tenant:acme, viewer, skill_path)
    → No copy, just permission grant
```

**Sharing Examples:**
```python
skill_path = "/acme/alice/skills/my-skill"

# Share with entire tenant
skills_share(skill_path, "tenant", context)

# Share publicly (all users)
skills_share(skill_path, "public", context)

# Share with specific user
skills_share(skill_path, "user:bob@example.com", context)

# Share with specific agent
skills_share(skill_path, "agent:code-assistant", context)
```

**Revoking Access:**
```python
skills_unshare("/acme/alice/skills/my-skill", "user:bob@example.com", context)
    → rebac_delete(user:bob, viewer, skill_path)
```

### 3. Runner (Agent Using Skills)

**Agent Configuration (stored in `/<tenant>/<user>/agents/<agent_id>/config.yaml`):**
```yaml
# Agent config uses full paths to avoid ambiguity
active_skills:
  - "/acme/alice/skills/code-review"    # Alice's version
  - "/acme/bob/skills/code-review"      # Bob's version (different!)
  - "/acme/alice/skills/testing"
```

**Phase 1: System Prompt Injection**
```
Agent session starts
    ↓
skills_get_prompt_context(context)
    ↓
Returns only skills agent has read permission on AND are in active_skills
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
write("/<tenant>/<user>/skills/<name>/SKILL.md", content)

# Read skill
read("/<tenant>/<user>/skills/<name>/SKILL.md")

# Delete skill
delete("/<tenant>/<user>/skills/<name>/")

# List skills
list("/<tenant>/<user>/skills/")
```

### New Skill-Specific APIs

These APIs handle operations that go beyond basic filesystem CRUD:

```python
@rpc_expose
def skills_share(
    skill_path: str,  # Full path like "/acme/alice/skills/code-review"
    share_with: str,  # "tenant" | "public" | "user:X" | "agent:X"
    context: OperationContext,
) -> dict:
    """Share skill by granting read permission.

    Args:
        skill_path: Full path to the skill
        share_with: Target to share with
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
def skills_get_prompt_context(
    context: OperationContext,
    max_skills: int = 50,
) -> dict:
    """Get skills metadata for system prompt injection.

    Returns only skills the caller has read permission on
    AND are in the agent's active_skills config.
    Optimized for low token count (~100 tokens/skill).

    Returns:
        {
            "xml": "<available_skills>...</available_skills>",
            "skills": [
                {
                    "name": "code-review",
                    "owner": "alice",           # Owner for disambiguation
                    "description": "...",
                    "path": "/acme/alice/skills/code-review"
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
        skill_path: Full path like "/acme/alice/skills/code-review"

    Checks read permission before returning content.

    Returns:
        {
            "name": "code-review",
            "owner": "alice",
            "path": "/acme/alice/skills/code-review",
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
skill_path = "/acme/alice/skills/my-skill"

# Submit for approval (unchanged)
skills_submit_approval(skill_path, target="tenant", reviewers=["admin@acme.com"])

# Reviewer approves
skills_approve(approval_id)
    → System calls: skills_share(skill_path, "tenant", system_context)
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
| API surface | ❌ Skill-specific CRUD | ✅ Filesystem-native |
| Draft handling | ❌ Separate state | ✅ Private = draft |

## Implementation Plan

### Phase 0: System Skills

1. Create `/system/skills/skill-creator/SKILL.md` - conversational skill building guide
2. Ensure system skills have `role:public` viewer permission

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

- [ ] `skill-creator` system skill exists and guides conversational building
- [ ] Skills created via filesystem `write()` are private by default
- [ ] Creator automatically gets owner permission on skill creation
- [ ] `skills_get_prompt_context()` returns only permitted skills
- [ ] `skills_load()` respects read permissions
- [ ] `skills_share()` grants correct permissions
- [ ] `skills_unshare()` revokes access correctly
- [ ] Governance approval grants permission (no copy)
- [ ] Progressive disclosure: metadata ~100 tokens, full content <5000 tokens
- [ ] Backward compatible: existing APIs work during transition
- [ ] All existing skill tests pass

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
write()  - create/update  |  skills_share()    - grant access
read()   - read           |  skills_unshare()  - revoke access
delete() - delete         |  skills_get_prompt_context() - discovery
list()   - list           |  skills_load()     - formatted read
```

### Draft State (Resolved)

**Q: How do we handle skill drafts for testing before publishing?**

A: No special draft state needed. "Draft" = private skill, "Published" = shared skill.

- New skills are private by default (only creator has access)
- Creator can test immediately since they have access
- "Publishing" is just sharing: `skills_share(path, "tenant")`
- No separate draft folder, no TTL cleanup, no complexity

## Open Questions

1. **Skill discovery performance**: How to efficiently discover all skills a user has permission to read?
2. **Permission inheritance**: Should skill subdirectories (scripts/, references/) inherit parent permissions?

## References

- [Agent Skills Specification](https://agentskills.io)
- Mount permission refactoring (PR #988)
