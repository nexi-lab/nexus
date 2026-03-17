#!/bin/bash
# Nexus ACP Tutorial — Calling Coding Agents
#
# Prerequisites:
#   1. nexusd running:  nexusd --port 2026  (gRPC on 2028 by default)
#   2. Agent binaries on PATH (claude, codex, or gemini)
#   3. export NEXUS_URL=http://localhost:2026
#
# Usage:
#   ./examples/tutorials/acp-coding-agents/acp_tutorial.sh

set -e

NEXUS=${NEXUS_BIN:-nexus}

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'

section() {
    echo ""
    echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
    echo ""
}

step() {
    echo -e "${GREEN}▸ $1${NC}"
}

run() {
    echo -e "${CYAN}\$ $*${NC}"
    "$@" 2>&1 || true
    echo ""
}

pause() {
    echo -e "${DIM}(press Enter to continue)${NC}"
    read -r
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
if [ -z "$NEXUS_URL" ]; then
    echo -e "${RED}NEXUS_URL is not set.${NC}"
    echo "  export NEXUS_URL=http://localhost:2026"
    exit 1
fi

# Find available agents
AGENTS=()
for candidate in gemini codex claude; do
    if command -v "$candidate" &>/dev/null; then
        AGENTS+=("$candidate")
    fi
done

if [ ${#AGENTS[@]} -eq 0 ]; then
    echo -e "${RED}No agent binary found on PATH (tried: gemini, codex, claude).${NC}"
    exit 1
fi

AGENT=${AGENTS[0]}
echo -e "${GREEN}Found ${#AGENTS[@]} agent(s): ${AGENTS[*]}${NC}"
echo -e "${GREEN}Primary agent for this tutorial: ${AGENT}${NC}"
echo ""

# ===================================================================
section "Step 1 — List available agents"
# ===================================================================
step "See which agents nexusd knows about:"
run $NEXUS acp agents
pause

# ===================================================================
section "Step 2 — View agent configuration"
# ===================================================================
step "Check the current config for '${AGENT}':"
run $NEXUS acp config -a "$AGENT"
pause

# ===================================================================
section "Step 3 — Set a system prompt"
# ===================================================================
step "Give the agent a personality:"
run $NEXUS acp system-prompt set -a "$AGENT" \
    -c "You are a concise coding assistant. Always reply in one sentence."

step "Verify it was saved:"
run $NEXUS acp system-prompt get -a "$AGENT"
pause

# ===================================================================
section "Step 4 — Call a single agent"
# ===================================================================
step "Ask a simple question:"
run $NEXUS acp call -a "$AGENT" -p "What is 2+2?" --timeout 60
pause

# ===================================================================
section "Step 5 — Call multiple agents in parallel"
# ===================================================================
if [ ${#AGENTS[@]} -ge 2 ]; then
    step "Fan out the same prompt to ${#AGENTS[@]} agents:"
    PIDS=()
    TMPDIR_PARA=$(mktemp -d)
    for ag in "${AGENTS[@]}"; do
        $NEXUS acp call -a "$ag" -p "What is the capital of France? One word." \
            --timeout 60 > "${TMPDIR_PARA}/${ag}.out" 2>&1 &
        PIDS+=($!)
    done
    # Wait and print results
    for i in "${!AGENTS[@]}"; do
        wait "${PIDS[$i]}" || true
        echo -e "${GREEN}[${AGENTS[$i]}]${NC}"
        cat "${TMPDIR_PARA}/${AGENTS[$i]}.out"
        echo ""
    done
    rm -rf "$TMPDIR_PARA"
else
    step "Only one agent available — skipping parallel demo."
fi
pause

# ===================================================================
section "Step 6 — Multi-turn session (resume)"
# ===================================================================
step "Start a conversation:"
OUTPUT=$($NEXUS acp call -a "$AGENT" -p "Remember the number 42." --timeout 60 2>&1)
echo "$OUTPUT"
echo ""

# Extract session ID from output
SESSION_ID=$(echo "$OUTPUT" | grep -oE 'session=[0-9a-f-]+' | head -1 | cut -d= -f2)

if [ -n "$SESSION_ID" ]; then
    step "Resume the session (${SESSION_ID:0:8}…) with a follow-up:"
    run $NEXUS acp call -a "$AGENT" -p "What number did I ask you to remember?" \
        -s "$SESSION_ID" --timeout 60
else
    echo -e "${YELLOW}Could not extract session ID — skipping resume.${NC}"
fi
pause

# ===================================================================
section "Step 7 — Process management"
# ===================================================================
step "List running ACP processes (should be empty after calls finish):"
run $NEXUS acp ps
pause

# ===================================================================
section "Step 8 — Call history"
# ===================================================================
step "Review recent calls:"
run $NEXUS acp history -n 10
pause

# ===================================================================
section "Step 9 — Clean up"
# ===================================================================
step "Clear the system prompt:"
run $NEXUS acp system-prompt set -a "$AGENT" -c ""

step "Verify it was cleared:"
run $NEXUS acp system-prompt get -a "$AGENT"

echo ""
echo -e "${GREEN}Tutorial complete! You've used every ACP CLI command.${NC}"
echo ""
echo -e "${DIM}Commands covered:${NC}"
echo -e "${DIM}  nexus acp agents${NC}"
echo -e "${DIM}  nexus acp config${NC}"
echo -e "${DIM}  nexus acp system-prompt get/set${NC}"
echo -e "${DIM}  nexus acp call (single, parallel, resume)${NC}"
echo -e "${DIM}  nexus acp ps${NC}"
echo -e "${DIM}  nexus acp history${NC}"
