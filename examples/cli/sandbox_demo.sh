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

sandbox_id=$(nexus sandbox create demo-sandbox --ttl 15 --json | jq -r '.sandbox_id')
echo "✓ Sandbox created: $sandbox_id"
echo

# Demo 2: Run Python code
echo "=== Demo 2: Run Python Code ==="
echo "Running Python code to check available packages..."
echo

nexus sandbox run "$sandbox_id" --language python --code "
import sys
import pandas as pd
import numpy as np

print(f'Python version: {sys.version}')
print(f'Pandas version: {pd.__version__}')
print(f'Numpy version: {np.__version__}')
print('\\n✓ All packages available!')
"
echo

# Demo 3: Run code from file
echo "=== Demo 3: Run Code from File ==="
echo "Creating a temporary Python script..."
echo

cat > /tmp/nexus_demo.py <<'PY'
# Data analysis example
import pandas as pd
import numpy as np

# Generate sample data
data = {
    'product': ['A', 'B', 'C', 'D', 'E'],
    'sales': [100, 150, 200, 175, 125],
    'profit': [20, 30, 45, 35, 25]
}

df = pd.DataFrame(data)

print("Sales Data:")
print(df)
print(f"\nTotal Sales: ${df['sales'].sum()}")
print(f"Total Profit: ${df['profit'].sum()}")
print(f"Profit Margin: {(df['profit'].sum() / df['sales'].sum() * 100):.1f}%")
PY

echo "Running script from file..."
echo

nexus sandbox run "$sandbox_id" --file /tmp/nexus_demo.py
echo

# Demo 4: Run JavaScript
echo "=== Demo 4: Run JavaScript Code ==="
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

# Demo 5: Run Bash commands
echo "=== Demo 5: Run Bash Commands ==="
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

# Demo 6: List sandboxes
echo "=== Demo 6: List Sandboxes ==="
echo "Listing all sandboxes..."
echo

nexus sandbox list
echo

# Demo 7: Get sandbox status
echo "=== Demo 7: Sandbox Status ==="
echo "Getting detailed status..."
echo

nexus sandbox status "$sandbox_id"
echo

# Demo 8: Cleanup
echo "=== Demo 8: Cleanup ==="
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
echo "  - Set up E2B template: e2b template build"
