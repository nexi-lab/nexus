# Standard vs Nexus: Side-by-Side Comparison

This document shows exactly what changes when you drop-in replace standard file I/O with Nexus.

## 1. Imports

### Standard Version
```python
import os
from typing import TypedDict, Literal
from langgraph.graph import StateGraph, START, END
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
```

### Nexus Version
```python
import os
from typing import TypedDict, Literal
from langgraph.graph import StateGraph, START, END
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from nexus.remote import RemoteNexusFS  # ← Only new import!
```

**Difference:** Add just ONE import: `RemoteNexusFS`

---

## 2. File Writing

### Standard Version
```python
os.makedirs("workspace/research", exist_ok=True)
research_file = "workspace/research/requirements.txt"

with open(research_file, "w") as f:
    f.write(requirements)
```

### Nexus Version
```python
# No makedirs needed - Nexus handles it automatically
research_file = "/workspace/research/requirements.txt"

nexus.write(research_file, requirements)
```

**Difference:**
- Replace `os.makedirs()` + `open()` + `f.write()` → `nexus.write()`
- Simpler, cleaner code
- Built-in permission checks

---

## 3. File Reading

### Standard Version
```python
with open(state["research_file"], "r") as f:
    requirements = f.read()
```

### Nexus Version
```python
requirements = nexus.read(state["research_file"])
```

**Difference:**
- Replace `open()` + `f.read()` → `nexus.read()`
- One line instead of two
- Automatic permission validation

---

## 4. Agent Setup (NEW with Nexus)

### Standard Version
```python
# No agent identification
# All code runs with same permissions
```

### Nexus Version
```python
# Connect as specific agent with restricted permissions
nexus = RemoteNexusFS(
    server_url=os.getenv("NEXUS_URL", "http://localhost:8080"),
    api_key=os.getenv("NEXUS_API_KEY")
)
nexus.agent_id = "researcher"  # Identifies agent for permission checks
```

**Difference:**
- Explicit agent identity
- Permission-based access control
- Each agent has different capabilities

---

## 5. Permission Setup (NEW with Nexus)

### Standard Version
```python
# No permissions - everyone can access everything
```

### Nexus Version
```python
# Granular permission control using ReBAC
admin_nx.rebac_create(
    subject=("agent", "researcher"),
    relation="direct_editor",
    object=("file", "/workspace/research")
)
# Researcher can ONLY write to /workspace/research/
```

**Difference:**
- Fine-grained access control
- Role-based permissions
- Audit trail of who accessed what

---

## 6. Complete Agent Node Comparison

### Standard Version (Researcher Node)
```python
def researcher_node(state: AgentState) -> AgentState:
    print(f"\n🔍 Researcher is analyzing task: {state['task']}")

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.7)
    messages = [...]
    response = llm.invoke(messages)
    requirements = response.content

    # Standard file I/O
    os.makedirs("workspace/research", exist_ok=True)
    research_file = "workspace/research/requirements.txt"
    with open(research_file, "w") as f:
        f.write(requirements)

    return {**state, "research_file": research_file, "current_agent": "coder"}
```

### Nexus Version (Researcher Node)
```python
def researcher_node(state: AgentState) -> AgentState:
    print(f"\n🔍 Researcher is analyzing task: {state['task']}")

    # Connect as researcher agent
    nexus = RemoteNexusFS(
        server_url=os.getenv("NEXUS_URL", "http://localhost:8080"),
        api_key=os.getenv("NEXUS_API_KEY")
    )
    nexus.agent_id = "researcher"

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.7)
    messages = [...]
    response = llm.invoke(messages)
    requirements = response.content

    # Nexus file I/O with permission checks
    research_file = "/workspace/research/requirements.txt"
    nexus.write(research_file, requirements)

    return {**state, "research_file": research_file, "current_agent": "coder"}
```

**Key Changes:**
1. Add `RemoteNexusFS` connection with agent identity
2. Replace `os.makedirs()` + `open()` + `write()` with `nexus.write()`
3. Use absolute paths (`/workspace/`) instead of relative paths
4. Permissions automatically enforced

---

## 7. Lines of Code Comparison

| Aspect | Standard Version | Nexus Version | Change |
|--------|------------------|---------------|--------|
| **Total Lines** | ~180 lines | ~310 lines | +130 lines |
| **Core Logic** | ~180 lines | ~180 lines | Same |
| **Permission Setup** | 0 lines | ~50 lines | New feature |
| **Permission Demo** | 0 lines | ~40 lines | New feature |
| **Documentation** | ~0 lines | ~40 lines | Better docs |

**Note:** The additional lines in Nexus version are:
- Permission setup code (demonstrates security features)
- Permission enforcement demo (proves it works)
- Enhanced documentation and error handling

The **core agent logic remains nearly identical** - just replacing file I/O calls!

---

## 8. What You Get With Nexus

### Security & Access Control
- ✅ Per-agent permission isolation
- ✅ Prevent unauthorized file access
- ✅ Audit trail of all operations
- ✅ Role-based access control (ReBAC)

### Developer Experience
- ✅ Simpler file I/O API (one line vs three)
- ✅ Automatic directory creation
- ✅ No more `open()`, `close()`, `with` statements
- ✅ Cloud-native storage

### Operations & Scale
- ✅ Multi-tenant support
- ✅ Distributed agent collaboration
- ✅ Persistent storage across environments
- ✅ Works in serverless/container environments

---

## 9. Migration Effort

To migrate from standard file I/O to Nexus:

1. **Add import** (1 line)
   ```python
   from nexus.remote import RemoteNexusFS
   ```

2. **Create Nexus connection** (3-4 lines per agent)
   ```python
   nexus = RemoteNexusFS(server_url=..., api_key=...)
   nexus.agent_id = "agent-name"
   ```

3. **Replace file operations** (1:1 replacement)
   - `open(path, "w")` + `f.write(data)` → `nexus.write(path, data)`
   - `open(path, "r")` + `f.read()` → `nexus.read(path)`
   - `os.makedirs()` → Not needed (automatic)

4. **Setup permissions** (optional, but recommended)
   ```python
   admin_nx.rebac_create(
       subject=("agent", "name"),
       relation="direct_editor",
       object=("file", "/path")
   )
   ```

**Total time:** ~30 minutes for a typical multi-agent workflow

---

## 10. Running the Examples

### Standard Version
```bash
export OPENAI_API_KEY="your-key"
python multi_agent_standard.py
```
Output: Local files in `./workspace/`

### Nexus Version
```bash
# Terminal 1: Start Nexus server
nexus serve --host localhost --port 8080

# Terminal 2: Run demo
export OPENAI_API_KEY="your-key"
export NEXUS_URL="http://localhost:8080"
./run_nexus_demo.sh
```
Output: Cloud files in Nexus at `/workspace/` with permission control

---

## Conclusion

**Nexus provides a drop-in replacement for file I/O with minimal code changes and massive security/scalability benefits.**

The same multi-agent workflow that takes ~180 lines with standard file I/O:
- Works with Nexus with ~30 lines of additional permission setup
- Core logic unchanged - just replace file I/O calls
- Gains enterprise-grade security, audit trails, and cloud storage
- Perfect for production LangGraph deployments!
