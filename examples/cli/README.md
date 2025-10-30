# Nexus CLI Demos

Interactive demos showcasing Nexus CLI features.

## Available Demos

### 1. ACE Learning System Demo (`ace_demo.sh`)

Demonstrates the **Agentic Context Engineering (ACE)** learning system:
- 📊 **Trajectory Tracking**: Record task execution with steps and outcomes
- 🧠 **Reflection**: Extract learnings from experience
- 📖 **Playbook Curation**: Build reusable strategies from reflections
- 🔄 **Re-learning Queue**: Continuous improvement on challenging tasks
- 💾 **Memory Operations**: Store and query knowledge

**Prerequisites:**
```bash
# 1. Start Nexus server with authentication
./scripts/init-nexus-with-auth.sh

# 2. Load admin credentials
source .nexus-admin-env
```

**Run:**
```bash
./examples/cli/ace_demo.sh

# Keep demo data for inspection
KEEP=1 ./examples/cli/ace_demo.sh
```

**What it demonstrates:**
- Creates 3 trajectories with different outcomes (success/partial/success)
- Runs batch reflection to extract common patterns
- Curates a playbook with learned strategies
- Shows memory storage and querying
- Processes re-learning queue

---

### 2. ReBAC Permissions Demo (`permissions_demo_enhanced.sh`)

Demonstrates **Relationship-Based Access Control (ReBAC)**:
- 👥 Multiple permission levels (owner/editor/viewer)
- 🏢 Group/team membership with relationship composition
- 📁 Permission inheritance through directory hierarchy
- 🔒 Multi-tenant isolation
- ⚡ Automatic cache invalidation
- 🔄 Move/rename permission retention

**Prerequisites:**
```bash
# 1. Start Nexus server with authentication
./scripts/init-nexus-with-auth.sh

# 2. Load admin credentials
source .nexus-admin-env
```

**Run:**
```bash
./examples/cli/permissions_demo_enhanced.sh

# Keep demo data for inspection
KEEP=1 ./examples/cli/permissions_demo_enhanced.sh
```

---

**Note:** Some ACE operations (batch_reflect, curate_playbook, memory storage) are not yet available via RPC and will show "NotImplementedError". Basic trajectory operations work via server mode.

---

## Server Setup

Both demos require a running Nexus server with authentication.

### Quick Start

```bash
# Start server (from nexus root)
./scripts/init-nexus-with-auth.sh

# In another terminal, load credentials
source .nexus-admin-env

# Run demos
./examples/cli/ace_demo.sh
./examples/cli/permissions_demo_enhanced.sh
```

### What `init-nexus-with-auth.sh` Does

1. Starts PostgreSQL (via Docker if needed)
2. Runs database migrations
3. Starts Nexus server with authentication
4. Creates admin user and API key
5. Exports credentials to `.nexus-admin-env`

### Verify Server is Running

```bash
# Check server health
curl http://localhost:8080/health

# Or use nexus CLI
source .nexus-admin-env
nexus ls /
```

---

## Demo Output

Both demos use color-coded output:
- 🟢 Green checkmarks (✓): Successful operations
- 🔵 Blue info (ℹ): Information messages
- 🟡 Yellow warnings (⚠): Warnings or optional steps
- 🔴 Red errors (✗): Errors
- 🔷 Cyan ($): Commands being executed

---

## Troubleshooting

### Error: "NEXUS_URL and NEXUS_API_KEY not set"

**Solution:** Load admin credentials first:
```bash
source .nexus-admin-env
```

### Error: "Connection refused"

**Solution:** Start the Nexus server:
```bash
./scripts/init-nexus-with-auth.sh
```

### Database errors

**Solution:** Run migrations:
```bash
export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost/nexus"
alembic upgrade head
```

### Want to inspect demo data?

**Solution:** Use `KEEP=1` flag:
```bash
KEEP=1 ./examples/cli/ace_demo.sh

# Then inspect
nexus memory trajectory list
nexus memory playbook list
```

---

## For Developers

### Adding a New Demo

1. Create `examples/cli/your_demo.sh`
2. Follow the same structure:
   - Color-coded print functions
   - Server prerequisite checks
   - Cleanup function with KEEP flag
   - Sections with clear descriptions
3. Make it executable: `chmod +x examples/cli/your_demo.sh`
4. Update this README

### Demo Script Template

```bash
#!/bin/bash
set -e

# Check prerequisites
if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    echo "Error: Run 'source .nexus-admin-env' first"
    exit 1
fi

# Your demo code here...

# Cleanup
cleanup() {
    if [ "$KEEP" != "1" ]; then
        # Cleanup code
    fi
}
trap cleanup EXIT
```
