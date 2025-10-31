#!/bin/bash
# Test MCP Server Example
#
# This script demonstrates how to test the Nexus MCP server locally.
# It starts the server and shows how to interact with it.

set -e

echo "=== Nexus MCP Server Test ==="
echo

# Set test data directory
export NEXUS_DATA_DIR="/tmp/nexus-mcp-test"

# Create test data
echo "1. Setting up test data..."
nexus init "$NEXUS_DATA_DIR"
nexus write /workspace/test.txt "Hello from MCP!" --data-dir "$NEXUS_DATA_DIR"
nexus write /workspace/code.py "def hello(): print('Hello')" --data-dir "$NEXUS_DATA_DIR"
echo "   ✓ Created test files"
echo

# Start MCP server in background (stdio mode for testing)
echo "2. Starting MCP server..."
echo "   Command: nexus mcp serve --transport stdio"
echo "   Note: In real usage, this would be started by Claude Desktop"
echo

# Show available tools
echo "3. Available MCP Tools:"
echo "   File Operations:"
echo "   - nexus_read_file"
echo "   - nexus_write_file"
echo "   - nexus_delete_file"
echo "   - nexus_list_files"
echo "   - nexus_file_info"
echo
echo "   Directory Operations:"
echo "   - nexus_mkdir"
echo "   - nexus_rmdir"
echo
echo "   Search:"
echo "   - nexus_glob"
echo "   - nexus_grep"
echo "   - nexus_semantic_search"
echo
echo "   Memory:"
echo "   - nexus_store_memory"
echo "   - nexus_query_memory"
echo
echo "   Workflows:"
echo "   - nexus_list_workflows"
echo "   - nexus_execute_workflow"
echo

echo "4. Example Usage in Claude Desktop:"
echo
echo "   User: 'Read the file at /workspace/test.txt'"
echo "   Claude: [calls nexus_read_file with path='/workspace/test.txt']"
echo "   Result: 'Hello from MCP!'"
echo
echo "   User: 'Find all Python files'"
echo "   Claude: [calls nexus_glob with pattern='**/*.py']"
echo "   Result: ['/workspace/code.py']"
echo
echo "   User: 'Remember that our API uses JWT tokens'"
echo "   Claude: [calls nexus_store_memory with content='API uses JWT tokens']"
echo "   Result: 'Successfully stored memory'"
echo

echo "5. Configuration for Claude Desktop:"
echo "   File: ~/.config/claude/claude_desktop_config.json"
echo "   Content:"
cat <<EOF
   {
     "mcpServers": {
       "nexus": {
         "command": "nexus",
         "args": ["mcp", "serve", "--transport", "stdio"],
         "env": {
           "NEXUS_DATA_DIR": "$NEXUS_DATA_DIR"
         }
       }
     }
   }
EOF
echo

echo "6. To actually start the MCP server:"
echo "   nexus mcp serve --transport stdio --data-dir $NEXUS_DATA_DIR"
echo

echo "=== Test Complete ==="
echo
echo "To clean up test data:"
echo "  rm -rf $NEXUS_DATA_DIR"
