#!/bin/bash
# E2B + Nexus Integration Demo (Pure Bash)
#
# This example demonstrates how to use Nexus filesystem within E2B sandboxes
# using only bash commands and the E2B CLI. Perfect for scripting and CI/CD.
#
# Prerequisites:
#   1. E2B CLI installed: npm install -g @e2b/cli
#   2. E2B template built: e2b template build -c examples/e2b/Dockerfile
#   3. E2B API key: export E2B_API_KEY=your-key
#   4. jq installed: brew install jq (or apt-get install jq)
#
# Usage:
#   export E2B_API_KEY=your-e2b-key
#   export NEXUS_API_KEY=your-nexus-key  # Optional
#   bash examples/e2b/e2b_demo.sh

set -e

# Configuration
NEXUS_URL="${NEXUS_URL:-http://nexus.sudorouter.ai}"
NEXUS_API_KEY="${NEXUS_API_KEY:-}"
E2B_TEMPLATE="${E2B_TEMPLATE:-nexus-sandbox-v1}"
MOUNT_PATH="/home/user/nexus"

# Check prerequisites
if ! command -v e2b &> /dev/null; then
    echo "Error: E2B CLI not installed"
    echo "Install with: npm install -g @e2b/cli"
    exit 1
fi

if ! command -v jq &> /dev/null; then
    echo "Error: jq not installed"
    echo "Install with: brew install jq (macOS) or apt-get install jq (Linux)"
    exit 1
fi

echo "Creating E2B sandbox with template: $E2B_TEMPLATE"

# Create sandbox and extract ID
SANDBOX_ID=$(e2b sandbox create --template "$E2B_TEMPLATE" | jq -r '.id')

if [ -z "$SANDBOX_ID" ] || [ "$SANDBOX_ID" = "null" ]; then
    echo "✗ Failed to create sandbox"
    exit 1
fi

echo "✓ Sandbox created: $SANDBOX_ID"

# Cleanup function
cleanup() {
    echo ""
    echo "Cleaning up..."
    e2b sandbox exec "$SANDBOX_ID" "nexus unmount $MOUNT_PATH 2>/dev/null || true"
    e2b sandbox delete "$SANDBOX_ID"
    echo "✓ Sandbox deleted"
}
trap cleanup EXIT

# Build mount command
MOUNT_CMD="sudo nexus mount $MOUNT_PATH --remote $NEXUS_URL --daemon --allow-other"
if [ -n "$NEXUS_API_KEY" ]; then
    MOUNT_CMD="$MOUNT_CMD --api-key $NEXUS_API_KEY"
fi

# Mount Nexus
echo ""
echo "Mounting Nexus from $NEXUS_URL..."
e2b sandbox exec "$SANDBOX_ID" "$MOUNT_CMD"
sleep 2

# Verify mount
echo ""
echo "Verifying mount..."
e2b sandbox exec "$SANDBOX_ID" "ls -la $MOUNT_PATH"

# Demo 1: Write file using echo
echo ""
echo "=== Demo 1: Write file using echo ==="
e2b sandbox exec "$SANDBOX_ID" "echo 'Hello from E2B sandbox!' > $MOUNT_PATH/workspace/hello.txt"
e2b sandbox exec "$SANDBOX_ID" "cat $MOUNT_PATH/workspace/hello.txt"

# Demo 2: Create multiple files and use grep
echo ""
echo "=== Demo 2: Search with grep ==="
e2b sandbox exec "$SANDBOX_ID" "echo 'TODO: Implement feature X' > $MOUNT_PATH/workspace/notes.txt"
e2b sandbox exec "$SANDBOX_ID" "echo 'DONE: Fixed bug Y' >> $MOUNT_PATH/workspace/notes.txt"
e2b sandbox exec "$SANDBOX_ID" "echo 'TODO: Write tests' >> $MOUNT_PATH/workspace/notes.txt"
echo "TODO items:"
e2b sandbox exec "$SANDBOX_ID" "grep TODO $MOUNT_PATH/workspace/notes.txt"

# Demo 3: Use find to locate files
echo ""
echo "=== Demo 3: Find files ==="
e2b sandbox exec "$SANDBOX_ID" "find $MOUNT_PATH/workspace -name '*.txt'"

# Demo 4: Word count
echo ""
echo "=== Demo 4: Word count ==="
e2b sandbox exec "$SANDBOX_ID" "wc -l $MOUNT_PATH/workspace/*.txt"

# Demo 5: Python script accessing mounted files
echo ""
echo "=== Demo 5: Python script with mounted files ==="
e2b sandbox exec "$SANDBOX_ID" "python3 <<'PYTHON'
import json

# Read from Nexus
with open('$MOUNT_PATH/workspace/hello.txt', 'r') as f:
    data = f.read()

# Process
result = {
    'input': data.strip(),
    'length': len(data),
    'uppercase': data.upper().strip()
}

# Write back to Nexus
with open('$MOUNT_PATH/workspace/processed.json', 'w') as f:
    json.dump(result, f, indent=2)

print('✓ Processed and saved to processed.json')
PYTHON"

# Show the result
echo "Output:"
e2b sandbox exec "$SANDBOX_ID" "cat $MOUNT_PATH/workspace/processed.json"

# Demo 6: Directory operations
echo ""
echo "=== Demo 6: Directory operations ==="
e2b sandbox exec "$SANDBOX_ID" "mkdir -p $MOUNT_PATH/workspace/data"
e2b sandbox exec "$SANDBOX_ID" "mv $MOUNT_PATH/workspace/*.txt $MOUNT_PATH/workspace/data/"
echo "Directory structure:"
e2b sandbox exec "$SANDBOX_ID" "ls -R $MOUNT_PATH/workspace"

# Demo 7: Git operations (if needed)
echo ""
echo "=== Demo 7: Git operations (optional) ==="
e2b sandbox exec "$SANDBOX_ID" "cd $MOUNT_PATH/workspace && git init && echo '*.log' > .gitignore"
e2b sandbox exec "$SANDBOX_ID" "cd $MOUNT_PATH/workspace && git status" || echo "Git demo completed"

echo ""
echo "=== All demos completed successfully! ==="
echo ""
echo "Key benefits of this approach:"
echo "  ✓ Use any standard Unix tool (ls, cat, grep, find, vim, etc.)"
echo "  ✓ No need to learn Nexus-specific CLI syntax"
echo "  ✓ Works with existing scripts and workflows"
echo "  ✓ Perfect for AI agents executing bash commands"
