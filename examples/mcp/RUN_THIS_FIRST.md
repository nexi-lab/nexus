# 🚀 Run MCP Examples in This Order

Follow these steps in sequence to learn and test the Nexus MCP implementation.

---

## Step 1: Understand Authentication Concepts (5 min)

**Run this first to understand how MCP authentication works:**

```bash
cd examples/mcp
./demo_mcp_auth_concept.sh
```

**What it does:**
- ✅ Explains authentication architecture
- ✅ Shows what works today
- ✅ Shows what needs enhancement
- ✅ No server required, just educational

**Expected output:** Clear explanation of MCP authentication

---

## Step 2: Verify Installation (1 min)

**Verify that MCP server can be created:**

```bash
python verify_mcp_tools.py
```

**What it does:**
- ✅ Creates a test Nexus instance
- ✅ Creates MCP server
- ✅ Lists all available tools
- ✅ Quick smoke test

**Expected output:**
```
=== Nexus MCP Server Verification ===

1. Creating Nexus instance...
   ✓ Nexus instance created

2. Creating MCP server...
   ✓ MCP server created

3. MCP Server Configuration:
   Server name: nexus
   Server type: FastMCP

4. Available Tools (based on implementation):
   ✓ nexus_read_file
   ✓ nexus_write_file
   ... (14 tools total)

=== Verification Complete ===
```

---

## Step 3: Test Basic MCP Server (2 min)

**Test the MCP server with sample data:**

```bash
./test_mcp_server.sh
```

**What it does:**
- ✅ Creates test Nexus workspace
- ✅ Writes sample files
- ✅ Shows all available MCP tools
- ✅ Displays usage examples
- ✅ Shows Claude Desktop configuration

**Expected output:**
```
=== Nexus MCP Server Test ===

1. Setting up test data...
✓ Initialized Nexus workspace at /tmp/nexus-mcp-test
✓ Created test files

2. Starting MCP server...
   Command: nexus mcp serve --transport stdio

3. Available MCP Tools:
   File Operations:
   - nexus_read_file
   - nexus_write_file
   ... (all 14 tools listed)

4. Example Usage in Claude Desktop:
   (shows examples)

5. Configuration for Claude Desktop:
   (shows config)
```

---

## Step 4: Try Claude Desktop Integration (5 min)

**Configure Claude Desktop to use Nexus MCP:**

1. Locate Claude Desktop config file:
   - **macOS**: `~/.config/claude/claude_desktop_config.json`
   - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
   - **Linux**: `~/.config/claude/claude_desktop_config.json`

2. Add Nexus MCP server:
   ```bash
   # Use the template
   cat claude_desktop_config.json
   ```

3. Edit the config (replace `/path/to/nexus-data` with your actual path):
   ```json
   {
     "mcpServers": {
       "nexus": {
         "command": "nexus",
         "args": ["mcp", "serve", "--transport", "stdio"],
         "env": {
           "NEXUS_DATA_DIR": "/Users/yourname/nexus-data"
         }
       }
     }
   }
   ```

4. Restart Claude Desktop (completely quit and relaunch)

5. In a new conversation, try:
   ```
   List all files in my Nexus workspace
   ```

**Expected:** Claude uses the `nexus_list_files` tool!

---

## Step 5: Test with Remote Nexus (Optional, 10 min)

**If you want to test with a remote Nexus server:**

### 5a. Start Nexus Server

```bash
# In terminal 1: Start Nexus server
nexus serve --host 127.0.0.1 --port 8080
```

### 5b. Connect MCP to Remote Server

```bash
# In terminal 2: Start MCP server pointing to remote
NEXUS_URL=http://127.0.0.1:8080 \
nexus mcp serve --transport stdio
```

### 5c. Test Operations

```bash
# In terminal 3: Test with CLI
export NEXUS_URL=http://127.0.0.1:8080

# Write a file through remote server
nexus write /test.txt "Hello from remote!"

# MCP server can now access this file
```

---

## Step 6: Authentication Demo (Optional, WIP)

**For authentication testing (work in progress):**

```bash
# This demonstrates authentication concepts but is still being refined
./test_mcp_with_auth.sh
```

**Note:** This test is a work in progress. The authentication architecture
is explained in `demo_mcp_auth_concept.sh` (Step 1).

---

## Quick Reference

### Start MCP Server for Claude Desktop
```bash
nexus mcp serve --transport stdio
```

### Start MCP Server for HTTP Clients
```bash
nexus mcp serve --transport http --port 8081
```

### Start MCP Server with Remote Nexus
```bash
NEXUS_URL=http://your-server:8080 \
NEXUS_API_KEY=your-api-key \
nexus mcp serve --transport stdio
```

### Check MCP Server Help
```bash
nexus mcp --help
nexus mcp serve --help
```

---

## Troubleshooting

### "Command not found: nexus"
```bash
# Make sure nexus is installed
pip install nexus-ai-fs

# Or if using uv
uv pip install nexus-ai-fs
```

### "MCP server not showing in Claude Desktop"
1. Check config file location is correct
2. Verify JSON syntax is valid
3. Completely quit and restart Claude Desktop (not just close window)
4. Check server logs if available

### "Permission denied" errors
```bash
# Make scripts executable
chmod +x demo_mcp_auth_concept.sh
chmod +x test_mcp_server.sh
chmod +x test_mcp_with_auth.sh
```

### "Module 'fastmcp' not found"
```bash
# Install fastmcp
pip install fastmcp
# or
uv pip install fastmcp
```

---

## Next Steps

After running these examples:

1. **Read the full documentation:**
   - CLI Reference: `../../docs/api/cli/mcp.md`
   - Integration Guide: `../../docs/integrations/mcp.md`

2. **Try building your own MCP client:**
   - See `basic_mcp_client.py` for example

3. **Customize for your use case:**
   - Add custom workflows
   - Set up semantic search
   - Configure ReBAC permissions

---

## Summary

| Step | Script | Time | Purpose |
|------|--------|------|---------|
| 1 | `demo_mcp_auth_concept.sh` | 5 min | Understand architecture |
| 2 | `verify_mcp_tools.py` | 1 min | Verify installation |
| 3 | `test_mcp_server.sh` | 2 min | Test MCP server |
| 4 | Claude Desktop setup | 5 min | Real usage |
| 5 | Remote server test | 10 min | Optional: Team setup |
| 6 | `test_mcp_with_auth.sh` | - | Optional: WIP |

**Total time to get started: ~15 minutes** ⚡

---

**Questions?** See the main README or documentation in `docs/integrations/mcp.md`
