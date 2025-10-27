# Delegation Support in Nexus ReBAC

**Question:** Does our permission system support delegation? (e.g., user creates agent, agent has user permissions for short time)

**Answer:** ✅ YES, with caveats

---

## TL;DR

Nexus ReBAC supports delegation through **two mechanisms**:

1. ✅ **Built-in:** Time-bounded relationships via `expires_at` (FULLY SUPPORTED)
2. ⚠️ **Custom:** Namespace-based delegation via `tupleToUserset` (POSSIBLE, NOT DEFAULT)

---

## 1. Time-Bounded Delegation (✅ Fully Supported)

### What It Is
Grant temporary permissions to an agent that automatically expire.

### How It Works

```python
from datetime import datetime, timedelta, UTC

# User alice creates agent for 1 hour
agent_id = "agent_claude_001"

# Grant agent TEMPORARY ownership of all alice's files
nx.rebac_create(
    subject=("agent", agent_id),
    relation="direct_owner",
    object=("file", "/alice/documents/report.pdf"),
    expires_at=datetime.now(UTC) + timedelta(hours=1)  # ← Auto-expires!
)
```

### Key Features

| Feature | Support | Details |
|---------|---------|---------|
| **Expiration** | ✅ YES | `expires_at` parameter on all tuples |
| **Auto-cleanup** | ✅ YES | `cleanup_expired_tuples()` removes expired relationships |
| **Filtering** | ✅ YES | Expired tuples never returned in queries |
| **Audit trail** | ✅ YES | Logged in `rebac_changelog` |

### Example: AI Agent with Temporary Access

```python
from datetime import datetime, timedelta, UTC

# Scenario: User alice asks AI agent to analyze her documents
print("Alice: Hey Claude, can you analyze my Q4 reports?")

# 1. Create agent session
agent_session_id = f"agent_claude_{int(datetime.now(UTC).timestamp())}"

# 2. Grant agent temporary access to alice's workspace
expires_in_1_hour = datetime.now(UTC) + timedelta(hours=1)

tuple_id = nx.rebac_create(
    subject=("agent", agent_session_id),
    relation="direct_viewer",  # Read-only access
    object=("file", "/alice/workspaces/q4-reports"),
    expires_at=expires_in_1_hour
)

print(f"✅ Agent {agent_session_id} granted read access for 1 hour")
print(f"   Expires: {expires_in_1_hour.isoformat()}")

# 3. Agent can now read alice's files (as agent, not alice)
context = OperationContext(
    subject_type="agent",
    subject_id=agent_session_id,
    groups=[],
    tenant_id="alice_tenant"
)

reports = nx.list("/alice/workspaces/q4-reports", subject=("agent", agent_session_id))
for report in reports:
    content = nx.read(report, subject=("agent", agent_session_id))
    analyze(content)  # Agent does analysis

# 4. After 1 hour, access automatically revoked
# No manual cleanup needed!
```

### Cleanup

```python
# Manual cleanup of expired tuples (optional)
nx._rebac_manager.cleanup_expired_tuples()

# Or set up periodic cleanup (recommended for production)
import schedule
schedule.every(1).hour.do(nx._rebac_manager.cleanup_expired_tuples)
```

---

## 2. Namespace-Based Delegation (⚠️ Possible, Not Default)

### What It Is
Agent **inherits** user's permissions automatically via `delegates-to` relationship.

### Current Status
⚠️ **PATTERN ONLY** - Requires custom namespace configuration (not included by default)

### How It Would Work

#### Step 1: Define Delegation Namespace

```python
from nexus.core.rebac import NamespaceConfig

DELEGATION_NAMESPACE = NamespaceConfig(
    namespace_id="delegation-namespace",
    object_type="file",
    config={
        "relations": {
            # Direct ownership
            "direct_owner": {},

            # Ownership via delegation
            "delegated_owner": {
                "tupleToUserset": {
                    # If user delegates-to agent, and user owns file,
                    # then agent has delegated_owner permission
                    "tupleset": "delegates-to",
                    "computedUserset": "owner"
                }
            },

            # Combined ownership (direct OR delegated)
            "owner": {
                "union": ["direct_owner", "delegated_owner", "parent_owner"]
            },

            # ... rest of namespace config
        },
        "permissions": {
            "read": ["viewer", "editor", "owner"],
            "write": ["editor", "owner"],
            "execute": ["owner"],
        }
    }
)

# Register namespace
nx._rebac_manager.create_namespace(DELEGATION_NAMESPACE)
```

#### Step 2: Create Delegation Relationship

```python
from datetime import datetime, timedelta, UTC

# Alice delegates to agent for 1 hour
expires_in_1_hour = datetime.now(UTC) + timedelta(hours=1)

delegation_tuple = nx.rebac_create(
    subject=("user", "alice"),
    relation="delegates-to",
    object=("agent", "claude_001"),
    expires_at=expires_in_1_hour
)

# Alice owns files
nx.rebac_create(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("file", "/alice/documents/report.pdf")
)
```

#### Step 3: Agent Automatically Inherits Permissions

```python
# Because:
# 1. alice delegates-to claude_001
# 2. alice owns /alice/documents/report.pdf
# 3. Namespace config: delegated_owner = delegates-to + owner
#
# Therefore: claude_001 has delegated_owner permission!

can_agent_read = nx.rebac_check(
    subject=("agent", "claude_001"),
    permission="read",
    object=("file", "/alice/documents/report.pdf")
)
# → True (via delegation!)
```

### Why Not Default?

1. **Security:** Delegation is powerful - need explicit opt-in
2. **Complexity:** `tupleToUserset` traversal adds graph complexity
3. **Use case specific:** Not all deployments need delegation

---

## 3. Comparison: Time-Bounded vs Namespace-Based

| Feature | Time-Bounded (✅ Default) | Namespace-Based (⚠️ Custom) |
|---------|--------------------------|------------------------------|
| **Explicit grants** | Per-file permissions | User-to-agent delegation |
| **Inheritance** | ❌ No (must grant each file) | ✅ Yes (inherits all user permissions) |
| **Setup** | None (works out of box) | Requires custom namespace |
| **Security** | Least privilege | Broader access |
| **Granularity** | Fine-grained | Coarse-grained |
| **Use case** | Specific file access | "Act as user" scenarios |

---

## 4. Real-World Examples

### Example A: AI Agent with Specific File Access (✅ Use Time-Bounded)

**Scenario:** User asks agent to analyze specific documents

```python
# User: "Claude, analyze my Q4 report and budget spreadsheet"

# Grant access to ONLY those 2 files
nx.rebac_create(
    subject=("agent", "claude_001"),
    relation="direct_viewer",
    object=("file", "/alice/q4-report.pdf"),
    expires_at=datetime.now(UTC) + timedelta(hours=1)
)

nx.rebac_create(
    subject=("agent", "claude_001"),
    relation="direct_viewer",
    object=("file", "/alice/budget.xlsx"),
    expires_at=datetime.now(UTC) + timedelta(hours=1)
)

# Agent ONLY has access to these 2 files (principle of least privilege)
```

### Example B: AI Assistant Acting as User (⚠️ Use Namespace-Based)

**Scenario:** AI assistant manages all user's files

```python
# User: "Claude, you're my personal assistant. Manage everything for me."

# Set up delegation namespace (one-time)
nx._rebac_manager.create_namespace(DELEGATION_NAMESPACE)

# Delegate all permissions to agent (temporary)
nx.rebac_create(
    subject=("user", "alice"),
    relation="delegates-to",
    object=("agent", "claude_assistant"),
    expires_at=datetime.now(UTC) + timedelta(days=30)  # 30 days
)

# Agent now inherits ALL alice's permissions automatically!
# Can read/write any file alice can access
```

---

## 5. Implementation Guide

### Option 1: Time-Bounded (Recommended for Most Use Cases)

```python
from datetime import datetime, timedelta, UTC

def grant_agent_temporary_access(
    nx,
    user_id: str,
    agent_id: str,
    files: list[str],
    duration_hours: int = 1,
    permission: str = "viewer"  # viewer, editor, or owner
):
    """Grant agent temporary access to specific files.

    Args:
        nx: NexusFS instance
        user_id: User granting access
        agent_id: Agent receiving access
        files: List of file paths
        duration_hours: How long access lasts
        permission: Level of access (viewer, editor, owner)

    Returns:
        List of tuple IDs (for revocation if needed)
    """
    expires_at = datetime.now(UTC) + timedelta(hours=duration_hours)
    tuple_ids = []

    for file_path in files:
        tuple_id = nx.rebac_create(
            subject=("agent", agent_id),
            relation=f"direct_{permission}",
            object=("file", file_path),
            expires_at=expires_at
        )
        tuple_ids.append(tuple_id)

    print(f"✅ Granted {agent_id} temporary {permission} access to {len(files)} files")
    print(f"   Expires: {expires_at.isoformat()}")

    return tuple_ids

# Usage
grant_agent_temporary_access(
    nx,
    user_id="alice",
    agent_id="claude_001",
    files=["/alice/report.pdf", "/alice/data.csv"],
    duration_hours=2,
    permission="viewer"
)
```

### Option 2: Namespace-Based (For Advanced Use Cases)

```python
from nexus.core.rebac import NamespaceConfig
from datetime import datetime, timedelta, UTC

def setup_delegation_system(nx):
    """One-time setup for delegation support."""

    DELEGATION_NAMESPACE = NamespaceConfig(
        namespace_id="delegation-enabled-file",
        object_type="file",
        config={
            "relations": {
                "direct_owner": {},
                "direct_editor": {},
                "direct_viewer": {},

                # Delegation support
                "delegated_owner": {
                    "tupleToUserset": {
                        "tupleset": "delegates-to",
                        "computedUserset": "owner"
                    }
                },
                "delegated_editor": {
                    "tupleToUserset": {
                        "tupleset": "delegates-to",
                        "computedUserset": "editor"
                    }
                },

                # Combined (direct OR delegated)
                "owner": {"union": ["direct_owner", "delegated_owner", "parent_owner"]},
                "editor": {"union": ["direct_editor", "delegated_editor", "owner"]},
                "viewer": {"union": ["direct_viewer", "editor"]},

                # Parent inheritance
                "parent_owner": {
                    "tupleToUserset": {
                        "tupleset": "parent",
                        "computedUserset": "owner"
                    }
                },
            },
            "permissions": {
                "read": ["viewer", "editor", "owner"],
                "write": ["editor", "owner"],
                "execute": ["owner"],
            }
        }
    )

    nx._rebac_manager.create_namespace(DELEGATION_NAMESPACE)
    print("✅ Delegation system enabled")

def delegate_to_agent(
    nx,
    user_id: str,
    agent_id: str,
    duration_hours: int = 24
):
    """Delegate all user permissions to agent temporarily.

    Args:
        nx: NexusFS instance
        user_id: User delegating permissions
        agent_id: Agent receiving delegation
        duration_hours: How long delegation lasts

    Returns:
        Tuple ID (for revocation if needed)
    """
    expires_at = datetime.now(UTC) + timedelta(hours=duration_hours)

    tuple_id = nx.rebac_create(
        subject=("user", user_id),
        relation="delegates-to",
        object=("agent", agent_id),
        expires_at=expires_at
    )

    print(f"✅ Delegated all {user_id} permissions to {agent_id}")
    print(f"   Expires: {expires_at.isoformat()}")
    print(f"   Agent now inherits ALL user permissions automatically")

    return tuple_id

# Usage
setup_delegation_system(nx)  # One-time
delegate_to_agent(nx, user_id="alice", agent_id="claude_assistant", duration_hours=24)
```

---

## 6. Security Considerations

### Time-Bounded Approach (Recommended)

✅ **Pros:**
- Principle of least privilege
- Explicit grants (easy to audit)
- Fine-grained control
- Default deny

❌ **Cons:**
- Requires granting each file individually
- More setup for large file sets

### Namespace-Based Approach

✅ **Pros:**
- Convenient ("act as user")
- Automatic inheritance
- Less setup

❌ **Cons:**
- Broader access (security risk)
- Harder to audit
- Requires custom namespace
- Complex graph traversal

---

## 7. Agent Creates Files - Key Pattern

### Question: When agent creates files, can the user see them?

**Answer: NO, not by default. The agent must explicitly grant the user access.**

### Pattern: Agent Creating Files FOR User

When an AI agent creates analysis files on behalf of a user, it should:

1. **Create the file**
2. **Grant itself ownership**
3. **Grant the user access** (viewer or owner)

```python
# Agent creates analysis file
analysis_path = "/alice/agent-analysis/summary.txt"
nx.write(analysis_path, b"AI analysis results...")

# Agent grants itself ownership
nx.rebac_create(
    subject=("agent", "claude_001"),
    relation="direct_owner",
    object=("file", analysis_path)
)

# IMPORTANT: Agent grants Alice access to see the results!
nx.rebac_create(
    subject=("user", "alice"),
    relation="direct_viewer",  # Or direct_owner to let Alice own it
    object=("file", analysis_path)
)

# Now Alice can read the analysis
content = nx.read(analysis_path, context=alice_context)
```

### Why This Matters

- ✅ **Security**: Alice can't see all agent files (principle of least privilege)
- ✅ **Flexibility**: Agent controls what user sees
- ⚠️ **Requirement**: Agent must be "delegation-aware" and explicitly share results

### Alternative: Grant User Ownership

If the agent creates files **on behalf of** the user (not just for the user), grant the user ownership instead:

```python
# Agent creates file but grants Alice ownership
nx.write(analysis_path, b"Analysis results...")

nx.rebac_create(
    subject=("user", "alice"),
    relation="direct_owner",  # Alice owns it, not agent
    object=("file", analysis_path)
)

# Agent can also grant itself viewer access if it needs to read later
nx.rebac_create(
    subject=("agent", "claude_001"),
    relation="direct_viewer",
    object=("file", analysis_path)
)
```

### Best Practice

**For AI assistants creating files FOR users:**
- Grant **user ownership** of created files
- Grant **agent temporary viewer** access (with expires_at)
- This way files belong to user, agent access expires automatically

---

## 8. Recommendations

### Use Time-Bounded When:
- ✅ Agent needs access to **specific files**
- ✅ Security is critical (least privilege)
- ✅ You want explicit control
- ✅ Audit trail must show exact permissions

### Use Namespace-Based When:
- ✅ Agent truly acts as **full user proxy**
- ✅ User explicitly delegates all permissions
- ✅ Convenience > fine-grained control
- ✅ You understand graph traversal implications

---

## 8. Summary

**Question:** Does our permission system support delegation?

**Answer:** ✅ **YES**, in two ways:

1. **Time-Bounded (✅ Recommended):** Grant specific file access with `expires_at`
2. **Namespace-Based (⚠️ Advanced):** Full user delegation via custom namespace

**Most use cases should use time-bounded delegation** for security and simplicity.

---

## References

- `src/nexus/core/rebac.py` - ReBACTuple with expires_at support
- `src/nexus/core/rebac_manager.py` - cleanup_expired_tuples()
- `examples/py_demo/rebac_advanced_demo.py:621` - Delegation demo
- `docs/design/permission-system.md` - Temporary access patterns

---
