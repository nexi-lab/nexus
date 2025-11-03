#!/usr/bin/env bash
#
# Sandbox Management Demo (Issue #372)
#
# Demonstrates Nexus-managed sandboxes for code execution using E2B.
#
# Prerequisites:
#   1. E2B API key: export E2B_API_KEY=your-key
#   2. E2B template ID: export E2B_TEMPLATE_ID=your-template-id (optional)
#   3. Nexus server running with E2B configured
#
# Usage:
#   ./examples/cli/sandbox_demo.sh

set -e  # Exit on error

# Configure Nexus server connection
export NEXUS_URL='https://nexus-dev.nexilab.co/api'
export NEXUS_API_KEY='sk-default_admin_d38a7427_244c5f756dcc064eea6e68a64aa2111e'

echo "=== Nexus Sandbox Management Demo ==="
echo

# Check for E2B API key
if [ -z "$E2B_API_KEY" ]; then
    echo "❌ E2B_API_KEY not set"
    echo "   Get your API key from https://e2b.dev"
    echo "   Then run: export E2B_API_KEY=your-key"
    exit 1
fi

echo "✓ E2B API key configured"
echo

# Demo 1: Create a sandbox
echo "=== Demo 1: Create Sandbox ==="
echo "Creating a new sandbox with 15-minute TTL..."
echo

# Use timestamp to ensure unique name
sandbox_name="demo-sandbox-$(date +%s)"
sandbox_id=$(nexus sandbox create "$sandbox_name" --ttl 15 --json | jq -r '.sandbox_id')
echo "✓ Sandbox created: $sandbox_id"
echo

# Demo 2: Run Python code
echo "=== Demo 2: Run Python Code ==="
echo "Running Python code in sandbox..."
echo

nexus sandbox run "$sandbox_id" --language python --code "
import sys
import json
import os

print(f'Python version: {sys.version}')
print(f'OS: {os.uname().sysname} {os.uname().release}')

# Simple data processing
data = {'name': 'Alice', 'age': 30, 'city': 'NYC'}
print(f'\\nData: {json.dumps(data, indent=2)}')
print('\\n✓ Python execution successful!')
"
echo

# Demo 3: Mount Nexus filesystem
echo "=== Demo 3: Mount Nexus Filesystem ==="

# Check if NEXUS_URL is set and publicly accessible
if [ -z "$NEXUS_URL" ] || [[ "$NEXUS_URL" == *"localhost"* ]] || [[ "$NEXUS_URL" == *"127.0.0.1"* ]]; then
    echo "⚠️  SKIPPING: Nexus server is running locally"
    echo "   E2B sandboxes need a publicly accessible Nexus server to mount"
    echo "   To enable mounting:"
    echo "     1. Use ngrok: ngrok http 8080"
    echo "     2. Set: export NEXUS_URL=https://your-ngrok-url.ngrok.io"
    echo "     3. Or deploy to production: export NEXUS_URL=https://nexus.sudorouter.ai"
    echo
else
    echo "Creating test files in Nexus..."
    echo

    # Use /tmp or root directory which admin should have access to
    nexus mkdir /sandbox-demo --parents || true
    nexus write /sandbox-demo/data.txt "Hello from Nexus!"
    nexus write /sandbox-demo/numbers.txt "1,2,3,4,5"
    nexus mkdir /sandbox-demo/subfolder --parents
    nexus write /sandbox-demo/subfolder/nested.txt "Nested file content"

    echo "Files created in Nexus"
    echo
    echo "Mounting Nexus filesystem into sandbox..."
    echo

    # Mount Nexus at /home/user/nexus
    nexus sandbox connect "$sandbox_id" --mount-path /home/user/nexus

    echo
    echo "Running code to access mounted files..."
    echo

    # Access files through the mount
    nexus sandbox run "$sandbox_id" --language python --code "
import os

mount_path = '/home/user/nexus'

print(f'Listing files in {mount_path}/sandbox-demo:')
try:
    for root, dirs, files in os.walk(f'{mount_path}/sandbox-demo'):
        level = root.replace(f'{mount_path}/sandbox-demo', '').count(os.sep)
        indent = ' ' * 2 * level
        print(f'{indent}{os.path.basename(root)}/')
        subindent = ' ' * 2 * (level + 1)
        for file in files:
            print(f'{subindent}{file}')
except Exception as e:
    print(f'Error listing files: {e}')

print()
print('Reading data.txt:')
try:
    with open(f'{mount_path}/sandbox-demo/data.txt', 'r') as f:
        print(f.read())
    print('✓ Nexus files accessible in sandbox!')
except Exception as e:
    print(f'Error reading file: {e}')
"
    echo
fi

# Demo 4: Run code from file
echo "=== Demo 4: Run Code from File ==="
echo "Creating a temporary Python script..."
echo

cat > /tmp/nexus_demo.py <<'PY'
# Data analysis example with built-in Python
import json

# Generate sample data
products = [
    {'name': 'A', 'sales': 100, 'profit': 20},
    {'name': 'B', 'sales': 150, 'profit': 30},
    {'name': 'C', 'sales': 200, 'profit': 45},
    {'name': 'D', 'sales': 175, 'profit': 35},
    {'name': 'E', 'sales': 125, 'profit': 25},
]

print("Sales Data:")
print(json.dumps(products, indent=2))

total_sales = sum(p['sales'] for p in products)
total_profit = sum(p['profit'] for p in products)
profit_margin = (total_profit / total_sales * 100)

print(f"\nTotal Sales: ${total_sales}")
print(f"Total Profit: ${total_profit}")
print(f"Profit Margin: {profit_margin:.1f}%")
PY

echo "Running script from file..."
echo

nexus sandbox run "$sandbox_id" --file /tmp/nexus_demo.py
echo

# Demo 5: Run JavaScript
echo "=== Demo 5: Run JavaScript Code ==="
echo "Running Node.js code..."
echo

nexus sandbox run "$sandbox_id" --language javascript --code "
const data = [1, 2, 3, 4, 5];
const sum = data.reduce((a, b) => a + b, 0);
const avg = sum / data.length;

console.log('Data:', data);
console.log('Sum:', sum);
console.log('Average:', avg);
console.log('✓ JavaScript execution successful!');
"
echo

# Demo 6: Run Bash commands
echo "=== Demo 6: Run Bash Commands ==="
echo "Running system commands..."
echo

nexus sandbox run "$sandbox_id" --language bash --code "
echo 'System Info:'
uname -a
echo
echo 'Disk Usage:'
df -h | head -5
echo
echo '✓ Bash execution successful!'
"
echo

# Demo 7: List sandboxes
echo "=== Demo 7: List Sandboxes ==="
echo "Listing all sandboxes..."
echo

nexus sandbox list
echo

# Demo 8: Get sandbox status
echo "=== Demo 8: Sandbox Status ==="
echo "Getting detailed status..."
echo

nexus sandbox status "$sandbox_id"
echo

# Demo 9: Cleanup
echo "=== Demo 9: Cleanup ==="
echo "Stopping sandbox..."
echo

nexus sandbox stop "$sandbox_id"
echo

# Cleanup temp file
rm -f /tmp/nexus_demo.py

echo "=== Demo Complete! ==="
echo
echo "Summary:"
echo "  ✓ Created sandbox"
echo "  ✓ Ran Python code (inline)"
echo "  ✓ Mounted Nexus filesystem"
echo "  ✓ Accessed Nexus files from sandbox"
echo "  ✓ Ran Python code (from file)"
echo "  ✓ Ran JavaScript code"
echo "  ✓ Ran Bash commands"
echo "  ✓ Listed sandboxes"
echo "  ✓ Retrieved sandbox status"
echo "  ✓ Stopped sandbox"
echo
echo "Next steps:"
echo "  - Try: nexus sandbox create --help"
echo "  - Try: nexus sandbox run --help"
echo "  - Try: nexus sandbox connect --help"
echo "  - Set up E2B template: e2b template build"
