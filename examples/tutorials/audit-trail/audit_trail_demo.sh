#!/bin/bash
# Nexus CLI - Audit Trail Demo
#
# Demonstrates the operation log (audit trail) feature:
# - Every write, delete, rename, mkdir, rmdir is recorded
# - Events persist even in CLI (single-command) mode
# - Query the audit log with filters
#
# ┌─────────────────────────────────────────────────────────────┐
# │ QUICK START                                                 │
# │                                                             │
# │ Option A: Local mode (standalone, no server needed)         │
# │   export NEXUS_DATABASE_URL="postgresql://..."              │
# │   ./examples/cli/audit_trail_demo.sh                        │
# │                                                             │
# │ Option B: Remote mode (with server)                         │
# │   Terminal 1: ./scripts/init-nexus-with-auth.sh             │
# │   Terminal 2: source .nexus-admin-env                       │
# │               ./examples/cli/audit_trail_demo.sh            │
# └─────────────────────────────────────────────────────────────┘
#
# Prerequisites:
# - PostgreSQL running (Docker or Homebrew)
# - Nexus installed: pip install nexus-ai-fs
# - NEXUS_DATABASE_URL set (for local mode)
#
# What This Demonstrates:
# - Write events are flushed to DB on CLI exit (Issue #2684)
# - Delete events are recorded in the audit log
# - nexus ops log shows all operations with filters

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

print_section() {
    echo ""
    echo "================================================================"
    echo -e "${BLUE}  $1${NC}"
    echo "================================================================"
}

print_step() {
    echo ""
    echo -e "${CYAN}> $1${NC}"
}

print_ok() {
    echo -e "${GREEN}  $1${NC}"
}

# ── Setup ────────────────────────────────────────────────────

print_section "Audit Trail Demo"

DEMO_DIR="/demo/audit-trail-$(date +%s)"

# ── Step 1: Write files ─────────────────────────────────────

print_section "Step 1: Write files"

print_step "nexus mkdir ${DEMO_DIR}"
nexus mkdir "${DEMO_DIR}" --parents

print_step "nexus write ${DEMO_DIR}/hello.txt \"Hello, audit trail!\""
nexus write "${DEMO_DIR}/hello.txt" "Hello, audit trail!"

print_step "nexus write ${DEMO_DIR}/data.json '{\"key\": \"value\"}'"
nexus write "${DEMO_DIR}/data.json" '{"key": "value"}'

print_ok "Wrote 2 files and created 1 directory"

# ── Step 2: Show the audit log ──────────────────────────────

print_section "Step 2: View audit log (all recent operations)"

print_step "nexus ops log --limit 10"
nexus ops log --limit 10

# ── Step 3: Filter by type ──────────────────────────────────

print_section "Step 3: Filter by operation type"

print_step "nexus ops log --type write --limit 5"
nexus ops log --type write --limit 5

print_step "nexus ops log --type mkdir --limit 5"
nexus ops log --type mkdir --limit 5

# ── Step 4: Delete a file and verify ────────────────────────

print_section "Step 4: Delete a file and check the log"

print_step "nexus rm ${DEMO_DIR}/hello.txt --yes"
nexus rm "${DEMO_DIR}/hello.txt" --yes 2>/dev/null || echo "y" | nexus rm "${DEMO_DIR}/hello.txt"

print_step "nexus ops log --type delete --limit 5"
nexus ops log --type delete --limit 5

# ── Step 5: Filter by path ──────────────────────────────────

print_section "Step 5: Filter by path"

print_step "nexus ops log --path ${DEMO_DIR}/ --limit 10"
nexus ops log --path "${DEMO_DIR}/" --limit 10

# ── Summary ─────────────────────────────────────────────────

print_section "Summary"

echo ""
echo "  The audit trail records every filesystem operation."
echo "  Events are persisted to the database even in CLI mode"
echo "  (single-command, no server), so nothing is lost."
echo ""
echo "  Useful commands:"
echo "    nexus ops log                       # all recent operations"
echo "    nexus ops log --type write          # filter by type"
echo "    nexus ops log --path /workspace/    # filter by path"
echo "    nexus ops log --agent my-agent      # filter by agent"
echo "    nexus ops log --status failure      # only failures"
echo "    nexus ops log --limit 100           # show more rows"
echo ""
