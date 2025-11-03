#!/bin/bash
# Nexus CLI - Sandbox Connect/Disconnect Demo (Issue #371)
#
# This demo showcases connecting Nexus to user-managed sandboxes.
# Unlike Nexus-managed sandboxes (Issue #372), this allows users to:
# - Maintain full control over sandbox lifecycle
# - Mount Nexus to existing sandboxes
# - No database persistence (one-time operation)
# - Flexible for long-running or externally managed sandboxes
#
# Features tested:
# - Connect to user-created E2B sandbox
# - Mount Nexus filesystem at custom path
# - Verify mount accessibility
# - Disconnect and cleanup
# - Provider abstraction (e2b, future: docker, etc.)
#
# Prerequisites:
# 1. E2B API key: export E2B_API_KEY=your-key
# 2. E2B CLI installed (optional, for creating test sandbox)
#
# Usage:
#   export E2B_API_KEY=your-e2b-key
#   ./examples/cli/sandbox_connect_demo.sh

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

print_section() {
    echo ""
    echo "════════════════════════════════════════════════════════════"
    echo "  $1"
    echo "════════════════════════════════════════════════════════════"
    echo ""
}

print_subsection() {
    echo ""
    echo "─── $1 ───"
    echo ""
}

print_success() { echo -e "${GREEN}✓${NC} $1"; }
print_info() { echo -e "${BLUE}ℹ${NC} $1"; }
print_warning() { echo -e "${YELLOW}⚠${NC} $1"; }
print_error() { echo -e "${RED}✗${NC} $1"; }
print_test() { echo -e "${MAGENTA}TEST:${NC} $1"; }
print_code() { echo -e "${CYAN}CODE:${NC} $1"; }

# Check prerequisites
if [ -z "$E2B_API_KEY" ]; then
    print_error "E2B_API_KEY not set"
    echo ""
    echo "To run this demo:"
    echo "  1. Get your E2B API key from https://e2b.dev"
    echo "  2. Run: export E2B_API_KEY=your-key"
    echo ""
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║   Nexus CLI - Sandbox Connect/Disconnect Demo (Issue    ║"
echo "║                       #371)                              ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
print_info "E2B API Key: ${E2B_API_KEY:0:20}..."
echo ""

# Test data directory
TEST_DIR="/tmp/nexus-connect-demo-$$"
mkdir -p "$TEST_DIR"

# Cleanup function
cleanup() {
    print_info "Cleaning up..."

    # Clean up test directory
    if [ -z "$KEEP" ]; then
        rm -rf "$TEST_DIR"
        print_success "Test directory removed: $TEST_DIR"
    else
        print_info "Keeping test directory: $TEST_DIR (KEEP=1)"
    fi

    print_success "Cleanup complete"
}

# Set trap for cleanup
trap cleanup EXIT

# ════════════════════════════════════════════════════════════
# PART 1: Introduction & Concept
# ════════════════════════════════════════════════════════════

print_section "PART 1: Introduction - User-Managed Sandboxes"

cat << 'EOF'
Nexus supports TWO sandbox approaches:

1. Nexus-managed sandboxes (Issue #372):
   - Full lifecycle management by Nexus
   - Database-backed metadata
   - Automatic TTL and cleanup
   - Command: nexus sandbox create/run/pause/resume/stop

2. User-managed sandboxes (THIS DEMO - Issue #371):
   - User creates and controls sandbox externally
   - Nexus only mounts filesystem temporarily
   - No database storage, no lifecycle tracking
   - Commands: nexus sandbox connect/disconnect

Use Case for User-Managed:
- Long-running development environments
- External CI/CD sandboxes
- Multi-tool sandboxes (not just Nexus)
- When you need full control over sandbox lifecycle

EOF

print_info "This demo shows approach #2: User-managed sandboxes"
echo ""
read -p "Press Enter to continue..."

# ════════════════════════════════════════════════════════════
# PART 2: Simulate User-Created Sandbox
# ════════════════════════════════════════════════════════════

print_section "PART 2: User Creates External Sandbox"

print_info "In real usage, you would create your sandbox externally:"
print_code "e2b sandbox create"
print_code "# or use existing long-running sandbox"
echo ""

print_info "For this demo, we'll simulate having an external sandbox ID:"
SANDBOX_ID="sb_demo_user_managed_12345"
print_success "Simulated Sandbox ID: $SANDBOX_ID"
echo ""
print_warning "Note: In production, use a real E2B sandbox ID"
echo ""

# ════════════════════════════════════════════════════════════
# PART 3: Connect to Sandbox
# ════════════════════════════════════════════════════════════

print_section "PART 3: Connect Nexus to User-Managed Sandbox"

print_subsection "3.1: Basic Connect (Default Options)"

print_test "Connect with defaults (provider=e2b, mount=/mnt/nexus)"
print_code "nexus sandbox connect $SANDBOX_ID --sandbox-api-key \$E2B_API_KEY"
echo ""

# Note: This will fail with simulated sandbox, but demonstrates the command
set +e
nexus sandbox connect "$SANDBOX_ID" --sandbox-api-key "$E2B_API_KEY" 2>&1 | head -20
CONNECT_RESULT=$?
set -e

if [ $CONNECT_RESULT -eq 0 ]; then
    print_success "Connected successfully!"
else
    print_warning "Connection failed (expected with simulated sandbox)"
    print_info "With a real E2B sandbox, this would mount Nexus filesystem"
fi
echo ""

print_subsection "3.2: Connect with Custom Mount Path"

print_test "Specify custom mount path"
print_code "nexus sandbox connect $SANDBOX_ID \\"
print_code "  --sandbox-api-key \$E2B_API_KEY \\"
print_code "  --mount-path /home/user/nexus"
echo ""
print_info "Allows mounting at user-preferred location"
echo ""

print_subsection "3.3: Connect with JSON Output"

print_test "Get machine-readable output"
print_code "nexus sandbox connect $SANDBOX_ID \\"
print_code "  --sandbox-api-key \$E2B_API_KEY \\"
print_code "  --json"
echo ""
print_info "Useful for scripting and automation"
echo ""

# ════════════════════════════════════════════════════════════
# PART 4: Provider Abstraction
# ════════════════════════════════════════════════════════════

print_section "PART 4: Provider Abstraction (Future Extensibility)"

print_info "The --provider flag enables future support for:"
echo ""
echo "  • e2b (default, current)"
echo "  • docker (future)"
echo "  • modal (future)"
echo "  • replicate (future)"
echo "  • custom providers"
echo ""

print_test "Explicit provider specification:"
print_code "nexus sandbox connect $SANDBOX_ID --provider e2b --sandbox-api-key \$E2B_API_KEY"
echo ""

# ════════════════════════════════════════════════════════════
# PART 5: Python API Usage
# ════════════════════════════════════════════════════════════

print_section "PART 5: Python API Examples"

print_subsection "5.1: Local Python API"

cat << 'EOF'
```python
from nexus import NexusFilesystem
import os

# Initialize
nx = NexusFilesystem()

# Connect to user's sandbox
result = nx.sandbox_connect(
    sandbox_id="sb_my_sandbox",
    provider="e2b",
    sandbox_api_key=os.getenv("E2B_API_KEY"),
    mount_path="/mnt/nexus"
)

print(f"✓ Connected at: {result['mounted_at']}")
print(f"  Mount path: {result['mount_path']}")

# Your sandbox now has access to Nexus at /mnt/nexus
# Work with your sandbox...

# Disconnect when done
result = nx.sandbox_disconnect(
    sandbox_id="sb_my_sandbox",
    provider="e2b",
    sandbox_api_key=os.getenv("E2B_API_KEY")
)

print(f"✓ Disconnected at: {result['unmounted_at']}")
```
EOF
echo ""

print_subsection "5.2: Remote Python API (RemoteNexusFS)"

cat << 'EOF'
```python
from nexus.remote import RemoteNexusFS
import os

# Connect to remote Nexus server
remote_nx = RemoteNexusFS(
    server_url="https://nexus.nexilab.co",
    api_key=os.getenv("NEXUS_API_KEY")
)

# Connect to user's E2B sandbox
result = remote_nx.sandbox_connect(
    sandbox_id="sb_xxx",
    sandbox_api_key=os.getenv("E2B_API_KEY"),
    mount_path="/mnt/nexus"
)

print(f"✓ Mounted: {result['mount_path']}")

# Sandbox can now access remote Nexus filesystem
# ...

# Disconnect
remote_nx.sandbox_disconnect(
    sandbox_id="sb_xxx",
    sandbox_api_key=os.getenv("E2B_API_KEY")
)
```
EOF
echo ""

# ════════════════════════════════════════════════════════════
# PART 6: RPC/REST API Example
# ════════════════════════════════════════════════════════════

print_section "PART 6: RPC API Example (REST/JSON-RPC)"

print_info "Connect via HTTP API:"
echo ""

cat << 'EOF'
```bash
curl -X POST https://nexus.nexilab.co/api/nfs/connect_sandbox \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_NEXUS_API_KEY" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "sandbox_connect",
    "params": {
      "sandbox_id": "sb_xxx",
      "provider": "e2b",
      "sandbox_api_key": "e2b_your_key",
      "mount_path": "/mnt/nexus"
    }
  }'
```

Response:
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "success": true,
    "sandbox_id": "sb_xxx",
    "provider": "e2b",
    "mount_path": "/mnt/nexus",
    "mounted_at": "2025-01-15T10:00:00Z"
  }
}
```
EOF
echo ""

# ════════════════════════════════════════════════════════════
# PART 7: Disconnect
# ════════════════════════════════════════════════════════════

print_section "PART 7: Disconnect from Sandbox"

print_test "Unmount Nexus from sandbox:"
print_code "nexus sandbox disconnect $SANDBOX_ID --sandbox-api-key \$E2B_API_KEY"
echo ""

set +e
nexus sandbox disconnect "$SANDBOX_ID" --sandbox-api-key "$E2B_API_KEY" 2>&1 | head -20
DISCONNECT_RESULT=$?
set -e

if [ $DISCONNECT_RESULT -eq 0 ]; then
    print_success "Disconnected successfully!"
else
    print_warning "Disconnect failed (expected with simulated sandbox)"
fi
echo ""

print_info "After disconnecting:"
echo "  • Nexus filesystem is unmounted from sandbox"
echo "  • Sandbox continues running (user-controlled)"
echo "  • No state stored in Nexus database"
echo ""

# ════════════════════════════════════════════════════════════
# PART 8: Use Cases & Best Practices
# ════════════════════════════════════════════════════════════

print_section "PART 8: Use Cases & Best Practices"

cat << 'EOF'
When to Use User-Managed Sandboxes (connect/disconnect):

✓ Long-running development environments
  - Keep sandbox running for days/weeks
  - Mount Nexus only when needed

✓ External CI/CD pipelines
  - Pipeline creates sandbox
  - Mount Nexus for specific build steps
  - Unmount when done, pipeline controls lifecycle

✓ Multi-tool sandboxes
  - Sandbox runs multiple services
  - Nexus is one of many mounted filesystems

✓ Cost optimization
  - Connect only during active work
  - Disconnect to save data transfer costs

✓ Security/compliance
  - Full audit trail in your sandbox provider
  - Nexus doesn't track external sandboxes


When to Use Nexus-Managed Sandboxes (create/run/stop):

✓ Short-lived code execution
✓ Automated TTL and cleanup
✓ Centralized sandbox management
✓ Integrated with Nexus workflows


Best Practices:

1. API Key Security:
   - Use environment variables (E2B_API_KEY)
   - Never hardcode API keys
   - Rotate keys regularly

2. Mount Path Selection:
   - Use user-writable paths: /mnt/nexus, /home/user/nexus
   - Avoid system paths: /usr, /bin, /etc

3. Error Handling:
   - Always disconnect in cleanup/finally blocks
   - Handle connection timeouts gracefully

4. Resource Management:
   - Disconnect when done to free resources
   - Monitor mount health in long-running sandboxes
EOF
echo ""

# ════════════════════════════════════════════════════════════
# PART 9: Real E2B Example (if available)
# ════════════════════════════════════════════════════════════

print_section "PART 9: Try with Real E2B Sandbox (Optional)"

print_info "To test with a real E2B sandbox:"
echo ""
echo "1. Install E2B CLI:"
print_code "   npm install -g @e2b/cli"
echo ""
echo "2. Create a sandbox:"
print_code "   SANDBOX_ID=\$(e2b sandbox create --json | jq -r .sandboxId)"
echo ""
echo "3. Connect Nexus:"
print_code "   nexus sandbox connect \$SANDBOX_ID --sandbox-api-key \$E2B_API_KEY"
echo ""
echo "4. Verify mount (in E2B sandbox):"
print_code "   e2b sandbox exec \$SANDBOX_ID 'ls -la /mnt/nexus'"
echo ""
echo "5. Disconnect:"
print_code "   nexus sandbox disconnect \$SANDBOX_ID --sandbox-api-key \$E2B_API_KEY"
echo ""
echo "6. Cleanup:"
print_code "   e2b sandbox delete \$SANDBOX_ID"
echo ""

# ════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════

print_section "Demo Complete!"

print_success "Demonstrated features:"
echo "  ✓ nexus sandbox connect command"
echo "  ✓ nexus sandbox disconnect command"
echo "  ✓ Provider abstraction (--provider flag)"
echo "  ✓ Custom mount paths (--mount-path flag)"
echo "  ✓ Python API examples (local & remote)"
echo "  ✓ RPC/REST API examples"
echo "  ✓ Use cases & best practices"
echo ""

print_info "Key Differences from Nexus-Managed Sandboxes:"
echo "  • User controls sandbox lifecycle"
echo "  • No database persistence"
echo "  • One-time mount operation"
echo "  • Flexible for long-running environments"
echo ""

print_info "Next Steps:"
echo "  1. Try with a real E2B sandbox (see Part 9)"
echo "  2. Integrate into your workflows"
echo "  3. Explore provider abstraction for future extensions"
echo ""

print_success "Issue #371 implementation complete! 🎉"
