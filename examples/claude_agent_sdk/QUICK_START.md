# Quick Start Guide: Claude Agent SDK + Nexus

Get up and running with Claude Agent SDK + Nexus in 5 minutes!

## Prerequisites

```bash
# Install Python packages
pip install nexus-ai-fs claude-agent-sdk

# Set Anthropic API key
export ANTHROPIC_API_KEY="your-anthropic-key"
```

## Option 1: Local Nexus (Easiest - No Server Needed)

Use Nexus directly without running a server:

```bash
# Just run the demo - it uses local Nexus by default
python claude_agent_react_demo.py
```

That's it! The demo will:
- Use local Nexus filesystem (no server needed)
- Store data in `./nexus-data/`
- Run the ReAct agent with file operations

## Option 2: Remote Nexus Server (Multi-User/Production)

For remote access or multi-user setups, use **database-backed authentication** (the proper way!):

### Step 1: Initialize Server with Database Auth

**Terminal 1** - Initialize and start server:

```bash
# Option A: Use the official init script (RECOMMENDED)
cd /path/to/nexus
./scripts/init-nexus-with-auth.sh

# Option B: Use our wrapper script
cd examples/claude_agent_sdk
./start_server_with_auth.sh
```

This will:
1. ✅ Create database schema
2. ✅ Create an **admin user** with an admin API key
3. ✅ Save admin key to `.nexus-admin-env`
4. ✅ Start server with `--auth-type database`

You'll see:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANT: Save this API key securely!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Admin API Key: nxk_1234abcd...

Add to your ~/.bashrc or ~/.zshrc:
  export NEXUS_API_KEY='nxk_1234abcd...'
  export NEXUS_URL='http://localhost:8080'
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Step 2: Create API Key for Claude Agent

**Terminal 2** - Create a user/agent with their own API key:

```bash
# Load admin credentials
source .nexus-admin-env

# Create API key for Claude agent (365 day expiry)
python3 ../../scripts/create-api-key.py claude-agent "Claude Agent Key" --days 365
```

You'll get:
```
✓ Created API key for user 'claude-agent'
  Name: Claude Agent Key
  Expires: 2026-01-30

IMPORTANT: Save this key - it will not be shown again!

  API Key: nxk_5678efgh...

Use with:
  export NEXUS_API_KEY='nxk_5678efgh...'
```

### Step 3: Run Claude Agent Demo

**Terminal 2** - Use the Claude agent's API key:

```bash
# Set Claude agent's credentials
export NEXUS_URL="http://localhost:8080"
export NEXUS_API_KEY="nxk_5678efgh..."  # The key you just created

# Run demo
python claude_agent_react_demo.py
```

### Alternative: Quick Test Mode (Not for Production)

For **testing only**, you can use a static API key:

```bash
# Terminal 1
./start_server.sh  # Uses static key "demo-key-12345"

# Terminal 2
export NEXUS_URL="http://localhost:8080"
export NEXUS_API_KEY="demo-key-12345"
python claude_agent_react_demo.py
```

⚠️ **Warning**: Static API keys are less secure. Use database auth for production!

### Step 3: Verify Connection

```bash
# Check server health
curl http://localhost:8080/health
# Should return: {"status":"ok"}

# Test from Python
python -c "
from nexus.remote import RemoteNexusFS
nx = RemoteNexusFS('http://localhost:8080', api_key='demo-key-12345')
nx.write('/test.txt', b'Hello from Claude!')
print('✓ Connected to Nexus server')
"
```

## Running the Demos

### 1. ReAct Agent Demo

Demonstrates Claude using Nexus for file operations:

```bash
python claude_agent_react_demo.py
```

**What it does**:
- Searches Python files for async/await patterns
- Reads file contents
- Writes a summary report to `/reports/async-patterns.md`

**Example output**:
```
[CLAUDE - Thinking]
I'll search for Python files with async/await patterns...

[CLAUDE - Tool Call]
→ grep_files("async def", "/workspace")

[TOOL RESULT]
Found 15 matches for 'async def' in /workspace:
  src/api.py: Line 23: async def fetch_data():
  src/worker.py: Line 45: async def process_task():
  ...
```

### 2. Memory Agent Demo

Demonstrates persistent agent memory using Nexus Memory API:

```bash
python memory_agent_demo.py
```

**What it does**:
- Stores facts, preferences, and experiences
- Recalls information using semantic search
- Persists memory across sessions

**Example conversation**:
```
You: My name is Alex and I'm a software engineer.
Claude: Nice to meet you, Alex! [stores fact]

You: I prefer Python over JavaScript.
Claude: Got it! I'll remember that preference. [stores preference]

You: What do you know about me?
Claude: [recalls memories] You're Alex, a software engineer who prefers Python...
```

## Common Server Commands

### Proper Way: Database-Backed Authentication

```bash
# 1. Initialize server (first time only)
./scripts/init-nexus-with-auth.sh

# This creates:
# - Admin user with admin API key (saved to .nexus-admin-env)
# - Database schema
# - Starts server with database auth

# 2. Create additional users/agents
source .nexus-admin-env  # Load admin credentials
python3 scripts/create-api-key.py alice "Alice's key" --days 90
python3 scripts/create-api-key.py claude-agent "Claude agent" --days 365

# 3. Each user uses their own API key
export NEXUS_API_KEY='nxk_...'  # From create-api-key.py output
export NEXUS_URL='http://localhost:8080'
nexus ls /workspace
```

### Quick Test Mode (Static Key)

For testing only - not recommended for production:

```bash
# Start with static key (old/simple way)
nexus serve --api-key "test-key-123"

# Or no auth (WARNING: open access!)
nexus serve

# Custom port/host
nexus serve --host 0.0.0.0 --port 9000

# Check server health
curl http://localhost:8080/health
```

## Server Configuration

### Environment Variables

```bash
# Server URL (for remote clients)
export NEXUS_URL="http://localhost:8080"

# API key for authentication
export NEXUS_API_KEY="your-key"

# Database URL (for database auth)
export NEXUS_DATABASE_URL="postgresql://user:pass@localhost/nexus"

# Data directory (for local backend)
export NEXUS_DATA_DIR="./nexus-data"

# Tenant ID (for multi-tenancy)
export NEXUS_TENANT_ID="my-tenant"

# Agent ID (for tracking)
export NEXUS_AGENT_ID="my-agent"
```

### Authentication Options

| Type | How to Set Up | Use Case |
|------|---------------|----------|
| **Database** (Recommended) | `./scripts/init-nexus-with-auth.sh` | Multi-user production, proper user management |
| **Static API Key** | `nexus serve --api-key "key"` | Quick testing, single static key (old way) |
| **None** | `nexus serve` | Local testing only (WARNING: no security!) |

**Key Differences**:

- **Database Auth**:
  - ✅ Each user/agent has their own API key
  - ✅ Admin can create/revoke keys
  - ✅ Keys can expire
  - ✅ Proper audit trail
  - Use: `create-api-key.py` to manage users

- **Static Key**:
  - ⚠️ One hardcoded key for everyone
  - ⚠️ Can't revoke without server restart
  - Use: Quick testing only

- **No Auth**:
  - ❌ No security at all
  - Use: Never in production!

## Troubleshooting

### "Connection refused" error

Server isn't running. Start it:
```bash
./start_server.sh
```

### "Authentication failed" error

Wrong API key. Make sure both server and client use same key:
```bash
# Server
nexus serve --api-key "my-key"

# Client
export NEXUS_API_KEY="my-key"
```

### "Module not found: claude_sdk"

Install the SDK:
```bash
pip install claude-agent-sdk
```

Note: Claude SDK requires Python 3.10+ and Node.js.

### "ANTHROPIC_API_KEY not set"

Set your API key:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Get a key from: https://console.anthropic.com/

### Server shows "WARNING: No authentication configured"

This is OK for local testing, but for production use:
```bash
nexus serve --api-key "your-secret-key"
```

## Next Steps

1. ✅ **Read COMPARISON.md** - Understand LangGraph vs Claude Agent SDK
2. ✅ **Modify the demos** - Customize tasks and tools
3. ✅ **Explore Nexus features**:
   - Memory API for persistent agent knowledge
   - Semantic search for RAG
   - Workflows for automation
   - Multi-tenancy for SaaS
4. ✅ **Build your own** - Use the patterns as templates

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│ Claude Agent SDK (Your Python Code)            │
│                                                 │
│  ┌──────────────────────────────────────────┐  │
│  │ query(prompt, tools=[...])               │  │
│  │   • grep_files()                         │  │
│  │   • read_file()                          │  │
│  │   • write_file()                         │  │
│  │   • store_memory()                       │  │
│  │   • recall_memory()                      │  │
│  └─────────────┬────────────────────────────┘  │
│                │                                │
└────────────────┼────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│ Nexus Filesystem                                │
│                                                 │
│  [Local Mode]              [Server Mode]       │
│  • Direct access           • Remote RPC        │
│  • ./nexus-data/           • http://server:8080│
│  • No server needed        • Multi-user        │
│                            • API key auth      │
│                                                 │
│  Features:                                      │
│  • File operations (read, write, grep, glob)   │
│  • Memory API (store, query, semantic search)  │
│  • Version control (time-travel, rollback)     │
│  • Multi-tenancy (isolated workspaces)         │
│  • Workflows (event-driven automation)         │
└─────────────────────────────────────────────────┘
```

## Support

- **Nexus Issues**: https://github.com/nexi-lab/nexus/issues
- **Claude Agent SDK**: https://github.com/anthropics/claude-agent-sdk-python
- **Documentation**: See README.md and COMPARISON.md in this directory

Happy building! 🚀
