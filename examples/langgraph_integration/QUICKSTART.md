# 🚀 Quick Start Guide

Get the LangGraph + Nexus demo running in 5 minutes!

## Option 1: Standard Version (No Setup)

```bash
# 1. Set API key
export OPENAI_API_KEY="your-openai-key"

# 2. Run it!
cd examples/langgraph_integration
python multi_agent_standard.py
```

**Result:** Creates files in `./workspace/` with no permission control.

---

## Option 2: Nexus Version (With Permissions) ⭐

### Prerequisites: PostgreSQL

The auth-enabled Nexus server requires PostgreSQL:

```bash
# macOS with Homebrew:
brew install postgresql
brew services start postgresql
createdb nexus
psql nexus -c "ALTER USER postgres WITH PASSWORD 'nexus';"

# Linux (Ubuntu/Debian):
sudo apt-get install postgresql
sudo -u postgres createdb nexus
sudo -u postgres psql -c "ALTER USER postgres WITH PASSWORD 'nexus';"
```

### First-Time Setup

**Step 1:** Initialize Auth-Enabled Server (in Terminal 1)

```bash
# Run from the nexus repo root
./scripts/init-nexus-with-auth.sh --init

# Type 'yes' when prompted
# This will:
# - Create PostgreSQL schema
# - Create admin user and API key
# - Save credentials to .nexus-admin-env
# - Start server at http://localhost:8080
```

**Step 2:** Run Demo (in Terminal 2)

```bash
# Set OpenAI key
export OPENAI_API_KEY="your-openai-key"

# Navigate to examples
cd examples/langgraph_integration

# Run the demo (auto-loads credentials from .nexus-admin-env)
./run_nexus_demo.sh
```

### Subsequent Runs (Easiest!)

After initial setup, the demo script automatically starts the server for you:

```bash
# Just set your key and run!
export OPENAI_API_KEY="your-key"
cd examples/langgraph_integration
./run_nexus_demo.sh

# The script will:
# - Check if server is running
# - Auto-start it in background if needed
# - Load credentials automatically
# - Run the demo
```

**Or manually control the server:**

```bash
# Terminal 1: Start server manually
./scripts/init-nexus-with-auth.sh

# Terminal 2: Run demo
cd examples/langgraph_integration
export OPENAI_API_KEY="your-key"
./run_nexus_demo.sh
```

**Result:** Creates files in Nexus with permission-based access control!

---

## What to Expect

### Demo Flow

1. **🔐 Permission Setup**
   ```
   ✓ Researcher can write to /research/
   ✓ Coder can read /research/ and write to /code/
   ✓ Reviewer can read /code/ and write to /reviews/
   ```

2. **🔍 Researcher Agent**
   - Analyzes task: "Create a simple calculator class..."
   - Generates requirements
   - Writes to `/workspace/research/requirements.txt`

3. **💻 Coder Agent**
   - Reads requirements from researcher
   - Implements the calculator class
   - Writes to `/workspace/code/implementation.py`

4. **📋 Reviewer Agent**
   - Reads code from coder
   - Provides code review feedback
   - Writes to `/workspace/reviews/review.txt`

5. **🔒 Permission Enforcement Demo**
   ```
   ❌ Test: Can reviewer write to /code/? → DENIED ✓
   ❌ Test: Can coder read /reviews/? → DENIED ✓
   ```

### Expected Output

```
════════════════════════════════════════════════════
Multi-Agent Workflow: Nexus with Permissions
════════════════════════════════════════════════════

🔐 Setting up Nexus permissions...
  ✓ Researcher can write to /research/
  ✓ Coder can read /research/ and write to /code/
  ✓ Reviewer can read /code/ and write to /reviews/
🔐 Permission setup complete!

📋 Starting task: Create a simple calculator class...

🔍 Researcher is analyzing task: Create a simple calculator class...
✓ Requirements written to /workspace/research/requirements.txt
  (Researcher has write permission to /workspace/research/)

💻 Coder is implementing solution...
  (Coder has read permission to /workspace/research/)
✓ Code written to /workspace/code/implementation.py
  (Coder has write permission to /workspace/code/)

📋 Reviewer is evaluating code...
  (Reviewer has read permission to /workspace/code/)
✓ Review written to /workspace/reviews/review.txt
  (Reviewer has write permission to /workspace/reviews/)

════════════════════════════════════════════════════
✅ Workflow completed!
════════════════════════════════════════════════════

Generated files:
  - Requirements: /workspace/research/requirements.txt
  - Code: /workspace/code/implementation.py
  - Review: /workspace/reviews/review.txt

════════════════════════════════════════════════════
🔒 Demonstrating Permission Enforcement
════════════════════════════════════════════════════

❌ Test: Can reviewer write to /code/? (Should be denied)
  ✓ Access denied: Permission denied

❌ Test: Can coder read /reviews/? (Should be denied)
  ✓ Access denied: Permission denied

🔒 Permission enforcement verified!
```

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'langchain_openai'"

Install dependencies:
```bash
uv pip install langgraph langchain-openai
```

### "Nexus auth credentials not set!"

You need to initialize the auth-enabled server first:
```bash
# From nexus repo root
./scripts/init-nexus-with-auth.sh --init

# Then load credentials
source .nexus-admin-env
```

### "Cannot connect to Nexus server"

Make sure the auth-enabled server is running:
```bash
# Check if server is running
curl http://localhost:8080/health

# If not, start it:
./scripts/init-nexus-with-auth.sh
```

### "PostgreSQL connection failed"

Ensure PostgreSQL is running and database exists:
```bash
# Check PostgreSQL status (macOS)
brew services list | grep postgresql

# Check if nexus database exists
psql -l | grep nexus

# If missing, create it:
createdb nexus
psql nexus -c "ALTER USER postgres WITH PASSWORD 'nexus';"
```

### "OPENAI_API_KEY not set"

Set your API key:
```bash
export OPENAI_API_KEY="sk-..."
```

Get one from: https://platform.openai.com/api-keys

### "Permission denied" errors in demo

This means permissions are working! The demo intentionally tests unauthorized access to show that ReBAC is enforcing permissions correctly.

---

## Next Steps

1. **Inspect Generated Files**
   ```bash
   # View files via Nexus CLI
   nexus cat /workspace/research/requirements.txt
   nexus cat /workspace/code/implementation.py
   nexus cat /workspace/reviews/review.txt
   ```

2. **Try Permission Tests**
   ```bash
   # Try to write as wrong agent (should fail)
   NEXUS_AGENT_ID=reviewer nexus write /workspace/code/hack.py "malicious"
   # Should be denied!
   ```

3. **Modify the Workflow**
   - Edit `multi_agent_nexus.py`
   - Change the task description
   - Add more agents (tester, deployer, etc.)
   - Customize permissions

4. **Compare with Standard**
   - Run `multi_agent_standard.py`
   - See how Nexus simplifies file I/O
   - Notice the security benefits

5. **Read the Docs**
   - `README.md` - Full documentation
   - `COMPARISON.md` - Side-by-side code comparison
   - `COMPARISON.md` - Migration guide

---

## Key Takeaways

✅ **Drop-in Replacement** - Minimal code changes to adopt Nexus
✅ **Permission Control** - Each agent has restricted access
✅ **Simpler API** - `nexus.write()` vs `open()` + context managers
✅ **Cloud-Native** - Works in distributed/serverless environments
✅ **Production-Ready** - Security, audit trails, multi-tenancy

**Perfect for enterprise LangGraph deployments!** 🚀
