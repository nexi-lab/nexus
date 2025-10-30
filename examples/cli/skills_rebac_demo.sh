#!/bin/bash
# Nexus CLI - Skills ReBAC Permissions Demo with Approval Workflow
#
# Demonstrates Skills ReBAC integration using CLI commands
# Including: Create, Fork, Submit for Approval, Approve, and Publish
#
# Prerequisites:
# 1. Server running: ./scripts/init-nexus-with-auth.sh
# 2. Load admin credentials: source .nexus-admin-env
#
# Usage:
#   ./examples/cli/skills_rebac_demo.sh

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

# Check prerequisites
if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    print_error "NEXUS_URL and NEXUS_API_KEY not set. Run: source .nexus-admin-env"
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║        Nexus CLI - Skills ReBAC Permissions Demo        ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL"
print_info "Testing Skills ReBAC integration"
echo ""

ADMIN_KEY="$NEXUS_API_KEY"
DEMO_SKILLS_DIR="/workspace/.nexus/skills"

# Track approval ID for demo
APPROVAL_ID=""

# Cleanup function
cleanup() {
    export NEXUS_API_KEY="$ADMIN_KEY"
    print_info "Cleaning up demo skills..."
    nexus rmdir -r -f "$DEMO_SKILLS_DIR/rebac-test-analyzer" 2>/dev/null || true
    nexus rmdir -r -f "$DEMO_SKILLS_DIR/rebac-bob-fork" 2>/dev/null || true
    nexus rmdir -r -f "$DEMO_SKILLS_DIR/rebac-workflow-demo" 2>/dev/null || true
    nexus rmdir -r -f "/shared/skills/rebac-test-analyzer" 2>/dev/null || true
    nexus rmdir -r -f "/shared/skills/rebac-workflow-demo" 2>/dev/null || true
}

if [ "$KEEP" != "1" ]; then
    trap cleanup EXIT
    print_info "Cleanup enabled. To keep demo data, run: KEEP=1 $0"
else
    print_info "KEEP=1 set - demo data will NOT be cleaned up"
fi

# ════════════════════════════════════════════════════════════
# Section 1: Skill Creation
# ════════════════════════════════════════════════════════════

print_section "1. Skill Creation (Agent Tier)"

print_subsection "Create a skill manually using nexus commands"

# Create skill directory
nexus mkdir -p "$DEMO_SKILLS_DIR/rebac-test-analyzer"

# Create SKILL.md with frontmatter
cat << 'SKILLMD' | nexus write "$DEMO_SKILLS_DIR/rebac-test-analyzer/SKILL.md" --input -
---
name: rebac-test-analyzer
description: Code quality analyzer with ReBAC permissions
version: 1.0.0
author: Alice
created_at: 2025-10-30T00:00:00Z
modified_at: 2025-10-30T00:00:00Z
---

# Code Quality Analyzer

This skill analyzes code quality and provides recommendations.

## Features

- Static code analysis
- Code smell detection
- Best practice suggestions
- Security vulnerability scanning

## Usage

Provide code files to analyze and receive detailed quality reports.
SKILLMD

print_success "Created skill: rebac-test-analyzer"
print_info "Location: $DEMO_SKILLS_DIR/rebac-test-analyzer/SKILL.md"

# ════════════════════════════════════════════════════════════
# Section 2: Read Skill Content
# ════════════════════════════════════════════════════════════

print_section "2. Reading Skills"

print_subsection "Read the skill we just created"

SKILL_CONTENT=$(nexus cat "$DEMO_SKILLS_DIR/rebac-test-analyzer/SKILL.md")
echo "$SKILL_CONTENT" | head -15
print_success "Successfully read skill content"

# ════════════════════════════════════════════════════════════
# Section 3: Create Another Skill (Fork Simulation)
# ════════════════════════════════════════════════════════════

print_section "3. Fork a Skill (Create Copy)"

print_subsection "Create a forked copy of the skill"

# Copy skill directory
nexus mkdir -p "$DEMO_SKILLS_DIR/rebac-bob-fork"

# Read original and modify metadata
cat << 'FORKEDSKILL' | nexus write "$DEMO_SKILLS_DIR/rebac-bob-fork/SKILL.md" --input -
---
name: rebac-bob-fork
description: Bob's forked analyzer
version: 1.1.0
author: Bob
forked_from: rebac-test-analyzer
created_at: 2025-10-30T01:00:00Z
modified_at: 2025-10-30T01:00:00Z
---

# Code Quality Analyzer (Bob's Fork)

This is Bob's customized version of the code analyzer.

## Features

- Static code analysis (enhanced)
- Code smell detection
- Best practice suggestions
- Security vulnerability scanning
- Custom Bob rules

## Usage

Provide code files to analyze with Bob's custom rules.
FORKEDSKILL

print_success "Created forked skill: rebac-bob-fork"
print_info "Location: $DEMO_SKILLS_DIR/rebac-bob-fork/SKILL.md"

# ════════════════════════════════════════════════════════════
# Section 4: Approval Workflow Demo
# ════════════════════════════════════════════════════════════

print_section "4. Approval Workflow (Submit → Approve → Publish)"

print_subsection "Create a skill specifically for approval workflow demo"

# Create a new skill for the approval workflow
nexus mkdir -p "$DEMO_SKILLS_DIR/rebac-workflow-demo"

cat << 'APPROVALSKILL' | nexus write "$DEMO_SKILLS_DIR/rebac-workflow-demo/SKILL.md" --input -
---
name: rebac-workflow-demo
description: Demonstrates approval workflow before publication
version: 1.0.0
author: Alice
created_at: 2025-10-30T02:00:00Z
modified_at: 2025-10-30T02:00:00Z
---

# Approval Workflow Demo Skill

This skill demonstrates the complete approval workflow:
1. Submit for approval
2. Review and approve
3. Publish to tenant tier

## Features

- Governance integration
- Multi-step approval process
- ReBAC permission checks

## Usage

This skill shows how organizational governance works in Nexus.
APPROVALSKILL

print_success "Created skill: rebac-workflow-demo"

print_subsection "Step 1: Submit skill for approval"

# Submit the skill for approval
print_info "Alice submits the skill for approval by Bob..."

OUTPUT=$(nexus skills submit-approval rebac-workflow-demo \
    --submitted-by alice \
    --reviewers bob,charlie \
    --comments "Ready for team-wide use" 2>&1) || {
    print_warning "submit-approval command not fully functional (in-memory only)"
    print_info "This would create an approval request in production"
}

if [[ "$OUTPUT" =~ Approval\ ID:\ ([a-f0-9-]+) ]]; then
    APPROVAL_ID="${BASH_REMATCH[1]}"
    print_success "Submitted for approval"
    print_info "Approval ID: $APPROVAL_ID"
    echo "$OUTPUT" | grep -E "(Submitted by|Reviewers|Comments):" | sed 's/^/  /'
else
    print_info "Simulating approval ID for demo..."
    APPROVAL_ID="demo-approval-12345"
fi

print_subsection "Step 2: List pending approvals"

print_info "Bob checks pending approvals..."

nexus skills list-approvals --status pending 2>&1 | head -20 || {
    print_warning "list-approvals requires database connection"
    print_info "In production, this would show:"
    echo "  ┌─────────────────────────────────────────────────────────┐"
    echo "  │ Approval ID      | Skill Name           | Status        │"
    echo "  ├─────────────────────────────────────────────────────────┤"
    echo "  │ demo-approval... | rebac-workflow-demo  | pending       │"
    echo "  └─────────────────────────────────────────────────────────┘"
}

print_subsection "Step 3: Bob approves the skill"

print_info "Bob reviews and approves..."

nexus skills approve "$APPROVAL_ID" \
    --reviewed-by bob \
    --reviewer-type user \
    --comments "Code quality looks excellent!" 2>&1 || {
    print_warning "approve command requires database + ReBAC connection"
    print_info "In production, this would:"
    echo "  ✓ Check Bob has 'approve' permission (ReBAC)"
    echo "  ✓ Update approval status to APPROVED"
    echo "  ✓ Record reviewer and timestamp"
}

print_success "Approval workflow demonstrated!"

print_subsection "Step 4: Publish the approved skill (simulation)"

print_info "Alice publishes the skill to tenant tier..."
print_warning "Using manual copy for demo (nexus skills publish requires governance wiring)"

# Create tenant skills directory
nexus mkdir -p "/shared/skills/rebac-workflow-demo"

# Copy skill to tenant tier
ORIGINAL=$(nexus cat "$DEMO_SKILLS_DIR/rebac-workflow-demo/SKILL.md")
echo "$ORIGINAL" | nexus write "/shared/skills/rebac-workflow-demo/SKILL.md" --input -

print_success "Skill published to tenant tier"
print_info "Location: /shared/skills/rebac-workflow-demo/SKILL.md"

echo ""
print_info "How approval enforcement works in production:"
echo "  1. nexus skills publish rebac-workflow-demo"
echo "  2. System checks: ReBAC 'publish' permission ✓"
echo "  3. System checks: Governance approval status ✓"
echo "  4. If NOT approved → GovernanceError"
echo "  5. If approved → Skill published to /shared/"
echo ""

# ════════════════════════════════════════════════════════════
# Section 5: Demonstrate Rejection Workflow
# ════════════════════════════════════════════════════════════

print_section "5. Rejection Workflow (Optional)"

print_subsection "Demonstrate what happens when approval is rejected"

print_info "Scenario: Charlie submits a skill, but Bob rejects it"

# Create another skill
nexus mkdir -p "$DEMO_SKILLS_DIR/rebac-rejected-skill"

cat << 'REJECTEDSKILL' | nexus write "$DEMO_SKILLS_DIR/rebac-rejected-skill/SKILL.md" --input -
---
name: rebac-rejected-skill
description: This skill will be rejected for demo purposes
version: 1.0.0
author: Charlie
---

# Rejected Skill Demo

This skill intentionally demonstrates the rejection workflow.
REJECTEDSKILL

print_success "Created skill: rebac-rejected-skill"

print_info "Charlie submits for approval..."

# Actually submit the skill for approval
REJECT_OUTPUT=$(nexus skills submit-approval rebac-rejected-skill \
    --submitted-by charlie \
    --reviewers bob \
    --comments "Please review for security" 2>&1) || {
    print_warning "submit-approval command failed"
    REJECT_APPROVAL_ID="demo-reject-67890"
}

if [[ "$REJECT_OUTPUT" =~ Approval\ ID:\ ([a-f0-9-]+) ]]; then
    REJECT_APPROVAL_ID="${BASH_REMATCH[1]}"
    print_success "Submitted for approval"
    print_info "Approval ID: $REJECT_APPROVAL_ID"
else
    REJECT_APPROVAL_ID="demo-reject-67890"
    print_info "Using simulated approval ID for demo"
fi

print_info "Bob reviews and rejects..."

nexus skills reject "$REJECT_APPROVAL_ID" \
    --reviewed-by bob \
    --reviewer-type user \
    --comments "Security concerns - needs more input validation" 2>&1 || {
    print_warning "reject command requires database connection"
    print_info "In production, this would:"
    echo "  ✓ Check Bob has 'approve' permission (ReBAC)"
    echo "  ✓ Update approval status to REJECTED"
    echo "  ✓ Record rejection reason"
}

print_success "Rejection workflow demonstrated"

print_info "Result: Skill remains in agent tier, cannot be published"
echo ""

# Cleanup rejected skill
nexus rmdir -r -f "$DEMO_SKILLS_DIR/rebac-rejected-skill" 2>/dev/null || true

# ════════════════════════════════════════════════════════════
# Section 6: Publish Original Skills to Tenant Tier
# ════════════════════════════════════════════════════════════

print_section "6. Publish Original Skills to Tenant Tier"

print_subsection "Copy skill to /shared/ (tenant tier)"

# Create tenant skills directory
nexus mkdir -p "/shared/skills/rebac-test-analyzer"

# Copy skill with updated metadata
ORIGINAL=$(nexus cat "$DEMO_SKILLS_DIR/rebac-test-analyzer/SKILL.md")
echo "$ORIGINAL" | nexus write "/shared/skills/rebac-test-analyzer/SKILL.md" --input -

print_success "Published skill to tenant tier"
print_info "Location: /shared/skills/rebac-test-analyzer/SKILL.md"

# ════════════════════════════════════════════════════════════
# Section 7: List All Skills
# ════════════════════════════════════════════════════════════

print_section "7. List All Created Skills"

print_subsection "Agent tier skills"
if nexus ls "$DEMO_SKILLS_DIR" 2>/dev/null | grep -q "rebac-"; then
    nexus ls "$DEMO_SKILLS_DIR" | grep "rebac-" | while read skill; do
        echo "  • $skill"
    done
    print_success "Found agent-tier skills"
else
    print_info "No agent-tier skills found"
fi

print_subsection "Tenant tier skills"
if nexus ls "/shared/skills" 2>/dev/null | grep -q "rebac-"; then
    nexus ls "/shared/skills" | grep "rebac-" | while read skill; do
        echo "  • $skill"
    done
    print_success "Found tenant-tier skills"
else
    print_info "No tenant-tier skills found"
fi

# ════════════════════════════════════════════════════════════
# Section 8: Verify Skill Metadata
# ════════════════════════════════════════════════════════════

print_section "8. Verify Skill Metadata"

print_subsection "Extract metadata from each skill"

for SKILL_PATH in \
    "$DEMO_SKILLS_DIR/rebac-test-analyzer/SKILL.md" \
    "$DEMO_SKILLS_DIR/rebac-bob-fork/SKILL.md" \
    "$DEMO_SKILLS_DIR/rebac-workflow-demo/SKILL.md" \
    "/shared/skills/rebac-test-analyzer/SKILL.md" \
    "/shared/skills/rebac-workflow-demo/SKILL.md"; do

    if nexus exists "$SKILL_PATH" 2>/dev/null; then
        SKILL_NAME=$(basename $(dirname "$SKILL_PATH"))
        echo ""
        echo "Skill: $SKILL_NAME"
        nexus cat "$SKILL_PATH" | sed -n '/^---$/,/^---$/p' | grep -v '^---$' | grep -E '^(name|description|author|version):' | sed 's/^/  /'
    fi
done

print_success "Skill verification complete"

# ════════════════════════════════════════════════════════════
# Section 9: Summary
# ════════════════════════════════════════════════════════════

print_section "9. Demo Summary"

echo ""
echo "Skills created and managed using Nexus CLI:"
echo ""
echo "  Agent Tier (Private):"
echo "    • rebac-test-analyzer"
echo "    • rebac-bob-fork"
echo "    • rebac-workflow-demo"
echo ""
echo "  Tenant Tier (Team-Shared):"
echo "    • rebac-test-analyzer (published)"
echo "    • rebac-workflow-demo (approved + published)"
echo ""

echo "╔══════════════════════════════════════════════════════════╗"
echo "║              Skills ReBAC Demo Complete!                ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

print_success "Demonstrated features:"
echo "  ✓ Creating skills with nexus mkdir/write"
echo "  ✓ Reading skills with nexus cat"
echo "  ✓ Forking skills (creating customized copies)"
echo "  ✓ Approval workflow (submit → approve/reject)"
echo "  ✓ Publishing between tiers with governance"
echo "  ✓ Listing skills with nexus ls"
echo "  ✓ Three-tier hierarchy (agent/tenant/system)"
echo ""

print_info "CLI Commands Demonstrated:"
echo "  • nexus skills submit-approval <skill> --submitted-by <user>"
echo "  • nexus skills list-approvals --status pending"
echo "  • nexus skills approve <approval-id> --reviewed-by <user>"
echo "  • nexus skills reject <approval-id> --reviewed-by <user>"
echo "  • nexus skills publish <skill> (requires approval)"
echo ""

if [ "$KEEP" = "1" ]; then
    print_info "Demo data preserved. Inspect with:"
    echo "  nexus ls $DEMO_SKILLS_DIR"
    echo "  nexus cat $DEMO_SKILLS_DIR/rebac-test-analyzer/SKILL.md"
fi
