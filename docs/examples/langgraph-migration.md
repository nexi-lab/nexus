# LangGraph Multi-Agent Migration Tutorial

**Transform your standard file-based LangGraph agents to use Nexus for permissions, cloud storage, and audit trails**

This tutorial shows exactly what changes you need to make to migrate a multi-agent LangGraph workflow from standard Python file I/O to Nexus. We'll use a real example: migrating a researcher→coder→reviewer pipeline.

---

## 🎯 What You'll Gain

By migrating to Nexus, your multi-agent workflow gains:

- ✅ **Permission-based access control** - Each agent has specific read/write permissions
- ✅ **Cloud-based file storage** - Work with remote Nexus servers
- ✅ **Audit trails** - Track all file operations
- ✅ **Multi-user collaboration** - Multiple agents sharing data safely
- ✅ **Drop-in replacement** - Minimal code changes required

---

## 📊 Before & After Comparison

### Standard File I/O (Before)
```python
import os

# Create directories
os.makedirs("workspace/research", exist_ok=True)

# Write file
with open("workspace/research/requirements.txt", "w") as f:
    f.write(requirements)

# Read file
with open("workspace/research/requirements.txt") as f:
    content = f.read()
```

### Nexus File System (After)
```python
import nexus

# Connect to Nexus
nx = nexus.connect(config={"mode": "remote", "url": "http://localhost:2026", "api_key": get_demo_user_key()})
nx.agent_id = "researcher"

# Write file (auto-creates directories, enforces permissions)
nx.write("/workspace/research/requirements.txt", requirements)

# Read file (permission-checked)
content = nx.read("/workspace/research/requirements.txt")
```

---

## 🔄 Migration Steps

### Step 1: Import Nexus

**Before:**
```python
import os
from typing import TypedDict
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
```

**After:**
```python
import os
from typing import TypedDict
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

# ✨ Add Nexus import
import nexus
```

---

### Step 2: Add Permission Setup Function

This is **NEW** - standard file I/O has no permissions concept.

```python
def setup_nexus_permissions(admin_nx: NexusFS, workspace: str):
    """
    Setup permission structure for multi-agent workflow.

    Demonstrates Nexus value-add:
    - Researcher: Can only write to /workspace/research/
    - Coder: Can read research, write to /workspace/code/
    - Reviewer: Can read code, write to /workspace/reviews/
    """
    print("\n🔐 Setting up Nexus permissions...")

    # Create workspace structure
    admin_nx.mkdir(f"{workspace}/research", parents=True)
    admin_nx.mkdir(f"{workspace}/code", parents=True)
    admin_nx.mkdir(f"{workspace}/reviews", parents=True)

    # Grant permissions for researcher agent
    admin_nx.rebac_create(
        subject=("agent", "researcher"),
        relation="direct_editor",
        object=("file", f"{workspace}/research")
    )
    print("  ✓ Researcher can write to /research/")

    # Grant permissions for coder agent
    admin_nx.rebac_create(
        subject=("agent", "coder"),
        relation="direct_viewer",
        object=("file", f"{workspace}/research")
    )
    admin_nx.rebac_create(
        subject=("agent", "coder"),
        relation="direct_editor",
        object=("file", f"{workspace}/code")
    )
    print("  ✓ Coder can read /research/ and write to /code/")

    # Grant permissions for reviewer agent
    admin_nx.rebac_create(
        subject=("agent", "reviewer"),
        relation="direct_viewer",
        object=("file", f"{workspace}/code")
    )
    admin_nx.rebac_create(
        subject=("agent", "reviewer"),
        relation="direct_editor",
        object=("file", f"{workspace}/reviews")
    )
    print("  ✓ Reviewer can read /code/ and write to /reviews/")

    print("🔐 Permission setup complete!\n")
```

---

### Step 3: Update Researcher Node

**Before:**
```python
def researcher_node(state: AgentState) -> AgentState:
    """Researcher agent: analyzes task and writes requirements."""
    print(f"\n🔍 Researcher is analyzing task: {state['task']}")

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.7)

    messages = [
        SystemMessage(content="You are a technical researcher..."),
        HumanMessage(content=f"Task: {state['task']}...")
    ]

    response = llm.invoke(messages)
    requirements = response.content

    # ❌ Standard file I/O
    os.makedirs("workspace/research", exist_ok=True)
    research_file = "workspace/research/requirements.txt"

    with open(research_file, "w") as f:
        f.write(requirements)

    print(f"✓ Requirements written to {research_file}")

    return {**state, "research_file": research_file, "current_agent": "coder"}
```

**After:**
```python
def researcher_node(state: AgentState) -> AgentState:
    """Researcher agent: analyzes task and writes requirements."""
    print(f"\n🔍 Researcher is analyzing task: {state['task']}")

    # ✨ Connect as researcher agent
    nx = nexus.connect(config={"mode": "remote", "url": os.getenv("NEXUS_URL", "http://localhost:2026"), "api_key": get_demo_user_key()})
    nx.agent_id = "researcher"  # Set agent identity

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.7)

    messages = [
        SystemMessage(content="You are a technical researcher..."),
        HumanMessage(content=f"Task: {state['task']}...")
    ]

    response = llm.invoke(messages)
    requirements = response.content

    # ✅ Nexus file system (drop-in replacement!)
    research_file = "/workspace/research/requirements.txt"
    nx.write(research_file, requirements)

    print(f"✓ Requirements written to {research_file}")
    print("  (Researcher has write permission to /workspace/research/)")

    return {**state, "research_file": research_file, "current_agent": "coder"}
```

**Key Changes:**
1. Added `nx = nexus.connect(...)` connection
2. Set `nx.agent_id = "researcher"` for permission enforcement
3. Replaced `os.makedirs()` → removed (Nexus auto-creates directories)
4. Replaced `open(file, "w")` → `nx.write(file, content)`
5. Changed path from relative `workspace/...` → absolute `/workspace/...`

---

### Step 4: Update Coder Node

**Before:**
```python
def coder_node(state: AgentState) -> AgentState:
    """Coder agent: reads requirements and writes code."""
    print("\n💻 Coder is implementing solution...")

    # ❌ Read requirements from file
    with open(state["research_file"]) as f:
        requirements = f.read()

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3)

    messages = [
        SystemMessage(content="You are an expert Python developer..."),
        HumanMessage(content=f"Requirements:\n{requirements}...")
    ]

    response = llm.invoke(messages)
    code = response.content

    # ❌ Write code to file
    os.makedirs("workspace/code", exist_ok=True)
    code_file = "workspace/code/implementation.py"

    with open(code_file, "w") as f:
        f.write(code)

    print(f"✓ Code written to {code_file}")

    return {**state, "code_file": code_file, "current_agent": "reviewer"}
```

**After:**
```python
def coder_node(state: AgentState) -> AgentState:
    """Coder agent: reads requirements and writes code."""
    print("\n💻 Coder is implementing solution...")

    # ✨ Connect as coder agent
    nx = nexus.connect(config={"mode": "remote", "url": os.getenv("NEXUS_URL", "http://localhost:2026"), "api_key": get_demo_user_key()})
    nx.agent_id = "coder"

    # ✅ Read requirements using Nexus
    requirements = nx.read(state["research_file"])
    print("  (Coder has read permission to /workspace/research/)")

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3)

    messages = [
        SystemMessage(content="You are an expert Python developer..."),
        HumanMessage(content=f"Requirements:\n{requirements}...")
    ]

    response = llm.invoke(messages)
    code = response.content

    # ✅ Write code using Nexus
    code_file = "/workspace/code/implementation.py"
    nx.write(code_file, code)

    print(f"✓ Code written to {code_file}")
    print("  (Coder has write permission to /workspace/code/)")

    return {**state, "code_file": code_file, "current_agent": "reviewer"}
```

**Key Changes:**
1. Added Nexus connection with `agent_id = "coder"`
2. Replaced `open(file).read()` → `nx.read(file)`
3. Replaced `open(file, "w")` → `nx.write(file, content)`
4. Added permission confirmation messages

---

### Step 5: Update Reviewer Node

**Before:**
```python
def reviewer_node(state: AgentState) -> AgentState:
    """Reviewer agent: reviews code and provides feedback."""
    print("\n📋 Reviewer is evaluating code...")

    # ❌ Read code from file
    with open(state["code_file"]) as f:
        code = f.read()

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.5)

    messages = [
        SystemMessage(content="You are a code reviewer..."),
        HumanMessage(content=f"Review this code:\n{code}")
    ]

    response = llm.invoke(messages)
    review = response.content

    # ❌ Write review to file
    os.makedirs("workspace/reviews", exist_ok=True)
    review_file = "workspace/reviews/review.txt"

    with open(review_file, "w") as f:
        f.write(review)

    print(f"✓ Review written to {review_file}")

    return {**state, "review_file": review_file, "current_agent": "end"}
```

**After:**
```python
def reviewer_node(state: AgentState) -> AgentState:
    """Reviewer agent: reviews code and provides feedback."""
    print("\n📋 Reviewer is evaluating code...")

    # ✨ Connect as reviewer agent
    nx = nexus.connect(config={"mode": "remote", "url": os.getenv("NEXUS_URL", "http://localhost:2026"), "api_key": get_demo_user_key()})
    nx.agent_id = "reviewer"

    # ✅ Read code using Nexus
    code = nx.read(state["code_file"])
    print("  (Reviewer has read permission to /workspace/code/)")

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.5)

    messages = [
        SystemMessage(content="You are a code reviewer..."),
        HumanMessage(content=f"Review this code:\n{code}")
    ]

    response = llm.invoke(messages)
    review = response.content

    # ✅ Write review using Nexus
    review_file = "/workspace/reviews/review.txt"
    nx.write(review_file, review)

    print(f"✓ Review written to {review_file}")
    print("  (Reviewer has write permission to /workspace/reviews/)")

    return {**state, "review_file": review_file, "current_agent": "end"}
```

---

### Step 6: Update Main Function

**Before:**
```python
def main():
    """Main function to run the multi-agent workflow."""
    print("=" * 60)
    print("Multi-Agent Workflow: Standard File I/O")
    print("=" * 60)

    # Initialize state
    initial_state: AgentState = {
        "task": "Create a simple calculator class...",
        "current_agent": "start",
        "research_file": "",
        "code_file": "",
        "review_file": "",
        "iteration": 0,
        "max_iterations": 1
    }

    # Build and run the graph
    graph = build_graph()
    result = graph.invoke(initial_state)

    print("\n" + "=" * 60)
    print("✅ Workflow completed!")
    print("=" * 60)
```

**After:**
```python
def main():
    """Main function to run the multi-agent workflow with Nexus."""
    print("=" * 60)
    print("Multi-Agent Workflow: Nexus with Permissions")
    print("=" * 60)

    # ✨ Setup admin connection for permission configuration
    admin_nx = nexus.connect(config={"mode": "remote", "url": os.getenv("NEXUS_URL", "http://localhost:2026"), "api_key": os.getenv("NEXUS_API_KEY")})

    workspace = "/workspace"

    # ✨ Clean up previous workspace
    try:
        admin_nx.rmdir(workspace, recursive=True)
    except:
        pass

    # ✨ Setup permissions (Nexus value-add!)
    setup_nexus_permissions(admin_nx, workspace)

    # Initialize state (same as standard version)
    initial_state: AgentState = {
        "task": "Create a simple calculator class...",
        "current_agent": "start",
        "research_file": "",
        "code_file": "",
        "review_file": "",
        "iteration": 0,
        "max_iterations": 1
    }

    # Build and run the graph (same as standard version)
    graph = build_graph()
    result = graph.invoke(initial_state)

    print("\n" + "=" * 60)
    print("✅ Workflow completed!")
    print("=" * 60)
    print("\nGenerated files:")
    print(f"  - Requirements: {result['research_file']}")
    print(f"  - Code: {result['code_file']}")
    print(f"  - Review: {result['review_file']}")
```

**Key Changes:**
1. Added admin Nexus connection for setup
2. Added workspace cleanup (optional)
3. Called `setup_nexus_permissions()` to configure access control
4. Added file listing at the end

---

## 📋 Complete Migration Checklist

- [ ] **Install Nexus**: `pip install nexus-ai-fs`
- [ ] **Start Nexus server**: `./scripts/init-nexus-with-auth.sh --init`
- [ ] **Create demo user**: `nexus admin create-user demo_user --name "Demo User"`
- [ ] **Register agents**: Register researcher, coder, reviewer as agents
- [ ] **Add Nexus import**: `import nexus`
- [ ] **Add permission setup function**: Copy `setup_nexus_permissions()`
- [ ] **Update each agent node**: Add Nexus connection + replace file I/O
- [ ] **Update main function**: Add permission setup
- [ ] **Set environment variables**: `NEXUS_URL`, `NEXUS_API_KEY`
- [ ] **Test permissions**: Verify agents can only access allowed paths

---

## 🧪 Testing Your Migration

### 1. Start Nexus Server

```bash
# Initialize with authentication
./scripts/init-nexus-with-auth.sh --init

# Or restart existing server
./scripts/init-nexus-with-auth.sh
```

### 2. Create Demo User & Agents

```bash
# Create non-admin user
source .nexus-admin-env
nexus admin create-user demo_user --name "Demo User"

# Register agents (via Python)
python3 << 'EOF'
from sqlalchemy import create_engine
from nexus.core.entity_registry import EntityRegistry

engine = create_engine("postgresql://postgres:nexus@localhost/nexus")
registry = EntityRegistry(engine)

for agent_id in ["researcher", "coder", "reviewer"]:
    registry.register_entity("agent", agent_id, "user", "demo_user")
    print(f"✓ Registered {agent_id}")
EOF
```

### 3. Run the Migrated Demo

```bash
cd examples/langgraph_integration
export OPENAI_API_KEY="your-key-here"
bash run_nexus_demo.sh
```

### 4. Verify Permissions Work

```bash
# Test that agents have proper access control
./test_agent_permissions.sh
```

Expected output:
```
✓ Test 1: Researcher can write to /workspace/research/
✓ Test 2: Researcher CANNOT write to /workspace/code/ (correctly denied)
✓ Test 3: Coder can read /research/ and write to /code/
✓ Test 4: Coder CANNOT read /workspace/reviews/ (correctly denied)
```

---

## 🎓 Key Differences Summary

| Feature | Standard File I/O | Nexus |
|---------|------------------|-------|
| **Directory creation** | `os.makedirs()` required | Auto-created |
| **File paths** | Relative (`workspace/...`) | Absolute (`/workspace/...`) |
| **Write operation** | `open(file, "w").write()` | `nx.write(file, content)` |
| **Read operation** | `open(file).read()` | `nx.read(file)` |
| **Permissions** | None (OS-level only) | Per-agent ReBAC permissions |
| **Agent identity** | Not tracked | `nx.agent_id = "researcher"` |
| **Remote access** | Local filesystem only | HTTP/HTTPS to remote server |
| **Audit trails** | None | All operations logged |
| **String handling** | Manual encode/decode | Auto-converts `str` → `bytes` |

---

## 💡 Best Practices

### 1. Use Absolute Paths

```python
# ❌ Bad - relative paths
nx.write("workspace/file.txt", content)

# ✅ Good - absolute paths
nx.write("/workspace/file.txt", content)
```

### 2. Set Agent ID Before Operations

```python
# ✅ Always set agent_id for permission enforcement
nx = nexus.connect(config={"mode": "remote", "url": "...", "api_key": "..."})
nx.agent_id = "researcher"  # Required!
```

### 3. Use Non-Admin Keys for Agents

```python
# ❌ Bad - admin key bypasses permissions
api_key = os.getenv("NEXUS_API_KEY")  # Admin key

# ✅ Good - non-admin user key
api_key = get_demo_user_key()  # Regular user
```

### 4. Setup Permissions Before Running Workflow

```python
# Always call setup_nexus_permissions() in main()
setup_nexus_permissions(admin_nx, workspace)
```

### 5. Handle Both Strings and Bytes

```python
# Nexus accepts both automatically
nx.write(file, "string content")  # Auto-converts to bytes
nx.write(file, b"bytes content")   # Direct bytes
```

---

## 🚀 Next Steps

1. **Run the examples**:
   - Standard: `python examples/langgraph_integration/multi_agent_standard.py`
   - Nexus: `bash examples/langgraph_integration/run_nexus_demo.sh`

2. **Explore more features**:
   - [Permission system](../PERMISSIONS.md)
   - [Authentication](../authentication.md)
   - [Multi-tenancy](../MULTI_TENANT.md)

3. **Build your own**:
   - Adapt this pattern to your workflows
   - Add more agents with custom permissions
   - Deploy to production with remote Nexus server

---

## 📚 Related Resources

- **Full Examples**:
  - [Standard version](https://github.com/nexi-lab/nexus/blob/main/examples/langgraph_integration/multi_agent_standard.py)
  - [Nexus version](https://github.com/nexi-lab/nexus/blob/main/examples/langgraph_integration/multi_agent_nexus.py)

- **Documentation**:
  - [LangGraph Integration Guide](./langgraph.md)
  - [Permissions Guide](./permissions.md)
  - [Getting Started](../getting-started/quickstart.md)

---

**Happy migrating!** 🎉 Questions? [Open an issue](https://github.com/nexi-lab/nexus/issues)
