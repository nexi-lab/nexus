# LangGraph + Nexus Integration Example

This example demonstrates how to **drop-in replace** standard file I/O with Nexus in LangGraph multi-agent workflows.

> **Note:** The Nexus version requires an **auth-enabled server** to demonstrate permission features. See setup instructions below.

## 🎯 What This Showcases

A multi-agent coding workflow with three agents:
- **Researcher**: Analyzes task and writes requirements
- **Coder**: Reads requirements and implements code
- **Reviewer**: Reviews code and provides feedback

## 📁 Files

### 1. `multi_agent_standard.py` - Standard File I/O
- Uses Python's built-in `open()`, `os.makedirs()`, etc.
- No access control - all agents can read/write everything
- Local file system only

### 2. `multi_agent_nexus.py` - Nexus Drop-in Replacement
- **Minimal code changes**: `open()` → `nexus.read()`, `write()` → `nexus.write()`
- **Permission-based access control**:
  - Researcher can only write to `/workspace/research/`
  - Coder can read research, write to `/workspace/code/`
  - Reviewer can read code, write to `/workspace/reviews/`
- Cloud-based storage with audit trails
- Includes permission enforcement demo

## 🚀 Quick Start

### Prerequisites

```bash
# 1. Install dependencies (if not already installed)
pip install langgraph langchain-openai nexus-ai-fs

# OR using uv (recommended):
uv pip install langgraph langchain-openai

# 2. Set up OpenAI API key
export OPENAI_API_KEY="your-key-here"

# 3. For Nexus version: Ensure PostgreSQL is running
# The auth-enabled server requires PostgreSQL for user/permission management
# Default connection: postgresql://postgres:nexus@localhost/nexus
#
# On macOS with Homebrew:
#   brew install postgresql
#   brew services start postgresql
#   createdb nexus
#   psql nexus -c "ALTER USER postgres WITH PASSWORD 'nexus';"
```

### Run Standard Version (No Nexus Required)

```bash
cd examples/langgraph_integration
python multi_agent_standard.py
```

**Output:** Creates local files in `./workspace/`:
```
workspace/
├── research/requirements.txt
├── code/implementation.py
└── reviews/review.txt
```

**Key Point:** All agents have unrestricted file access. No permission control.

---

### Run Nexus Version (With Permission Control)

The demo script will automatically start the server if needed. There are two scenarios:

#### First-Time Setup (New Installation)

**Step 1:** Initialize Nexus with Authentication (in a separate terminal)

```bash
# From repo root, initialize Nexus with auth
./scripts/init-nexus-with-auth.sh --init

# This will:
# - Set up PostgreSQL database
# - Create admin user and API key
# - Save credentials to .nexus-admin-env
# - Start server at http://localhost:8080

# Type 'yes' when prompted to confirm initialization
```

**Step 2:** Run the Demo (in another terminal)

```bash
cd examples/langgraph_integration

# Set OpenAI API key
export OPENAI_API_KEY="your-key-here"

# Run the demo (will automatically load credentials and connect to server)
./run_nexus_demo.sh
```

#### Subsequent Runs (Server Already Initialized)

If you've already initialized the server before, the demo script will automatically start it for you:

```bash
cd examples/langgraph_integration

# Set OpenAI API key (if not already set)
export OPENAI_API_KEY="your-key-here"

# Run the demo (will auto-start server in background if needed)
./run_nexus_demo.sh

# The script will:
# - Check if server is running
# - If not, start it automatically using existing credentials
# - Load credentials from .nexus-admin-env
# - Run the demo
```

**Manual Server Control (Optional)**

If you prefer to start the server manually:

```bash
# Start server (no data loss)
./scripts/init-nexus-with-auth.sh

# In another terminal, run the demo
cd examples/langgraph_integration
source ../.. /.nexus-admin-env  # Load credentials
./run_nexus_demo.sh
```

**Output:** Files created in Nexus at `/workspace/`:
- `/workspace/research/requirements.txt` (researcher can write)
- `/workspace/code/implementation.py` (coder can write)
- `/workspace/reviews/review.txt` (reviewer can write)

**Key Point:** Each agent has restricted permissions - demonstrated with enforcement tests!

## 🔄 Code Comparison

### Standard Version (Before)
```python
# Write file
os.makedirs("workspace/research", exist_ok=True)
with open("workspace/research/requirements.txt", "w") as f:
    f.write(requirements)

# Read file
with open("workspace/research/requirements.txt", "r") as f:
    content = f.read()
```

### Nexus Version (After)
```python
# Write file (with permission check!)
nexus.write("/workspace/research/requirements.txt", requirements)

# Read file (with permission check!)
content = nexus.read("/workspace/research/requirements.txt")
```

## 🔐 Permission Model

The Nexus version demonstrates role-based access:

| Agent      | Read Research | Write Research | Read Code | Write Code | Read Reviews | Write Reviews |
|------------|---------------|----------------|-----------|------------|--------------|---------------|
| Researcher | ❌            | ✅             | ❌        | ❌         | ❌           | ❌            |
| Coder      | ✅            | ❌             | ❌        | ✅         | ❌           | ❌            |
| Reviewer   | ❌            | ❌             | ✅        | ❌         | ❌           | ✅            |

## 💡 Key Takeaways

1. **Drop-in replacement**: Minimal code changes to integrate Nexus
2. **Security by default**: Permission-based access control per agent
3. **Cloud-native**: Files stored in Nexus, accessible from anywhere
4. **Audit trails**: Track which agent accessed/modified which files
5. **Multi-tenancy ready**: Easy to extend to multiple teams/projects

## 🎓 Use Cases

This pattern is valuable for:
- **Enterprise AI agents**: Need access control and compliance
- **Team collaboration**: Multiple users/agents working on shared codebase
- **Cloud deployments**: Stateless agents need persistent storage
- **Security-critical workflows**: Separation of duties between agents
- **Audit requirements**: Track all file access and modifications

## 📊 Next Steps

- Try modifying permissions to see enforcement in action
- Add more agents (tester, documenter, deployer)
- Implement approval workflows with human-in-the-loop
- Scale to multiple concurrent workflows with different permissions
