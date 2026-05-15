#!/bin/bash
# ============================================================================
# run_all.sh — Execute all 20 CLI scenario scripts sequentially
# ============================================================================
#
# Usage:
#   bash scripts/cli-scenarios/run_all.sh              # Run all 20 scenarios
#   bash scripts/cli-scenarios/run_all.sh 01 03 07 15  # Run specific ones
#
# Prerequisites:
#   1. Run 00_setup.sh first to start server, seed data, and start TUI
#   2. Ensure .env.scenarios exists (created by 00_setup.sh)
#
# Output:
#   - Per-scenario pass/fail summary
#   - Final aggregate report
#   - Log files in scripts/cli-scenarios/logs/

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

if [ ! -f "$SCRIPT_DIR/.env.scenarios" ]; then
    echo -e "${RED}ERROR:${NC} .env.scenarios not found."
    echo "       Run 00_setup.sh first."
    exit 1
fi

ALL_SCENARIOS=(
    "01_file_operations"
    "02_version_management"
    "03_agent_lifecycle"
    "04_access_control"
    "05_workspace_management"
    "06_search_discovery"
    "07_admin_users"
    "08_delegation"
    "09_zone_management"
    "10_batch_inspect"
    "11_stack_lifecycle"
    "12_profiles_config"
    "13_payments"
    "14_workflows"
    "15_mounts_connectors"
    "16_observability"
    "17_auth_identity"
    "18_memory_knowledge"
    "19_sandbox_acp_plugins"
    "20_infrastructure"
)

if [ $# -gt 0 ]; then
    SCENARIOS=()
    for num in "$@"; do
        padded=$(printf "%02d" "$num")
        for s in "${ALL_SCENARIOS[@]}"; do
            if [[ "$s" == "${padded}_"* ]]; then
                SCENARIOS+=("$s")
            fi
        done
    done
else
    SCENARIOS=("${ALL_SCENARIOS[@]}")
fi

echo ""
echo -e "${BOLD}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   Nexus CLI Scenario Test Suite — ${#SCENARIOS[@]} scenarios              ║${NC}"
echo -e "${BOLD}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
TOTAL_PASS=0
TOTAL_FAIL=0
RESULTS=()

for scenario in "${SCENARIOS[@]}"; do
    script="$SCRIPT_DIR/${scenario}.sh"
    logfile="$LOG_DIR/${scenario}_${TIMESTAMP}.log"

    echo -e "${CYAN}━━━ Running: ${scenario} ━━━${NC}"

    if [ ! -f "$script" ]; then
        echo -e "  ${RED}SKIP${NC} — script not found: $script"
        RESULTS+=("SKIP  $scenario")
        continue
    fi

    rc=0
    bash "$script" 2>&1 | tee "$logfile" || rc=$?

    pass=$(grep -c '\[PASS\]' "$logfile" 2>/dev/null || echo "0")
    fail=$(grep -c '\[FAIL\]' "$logfile" 2>/dev/null || echo "0")
    # Ensure numeric (grep -c can return empty or multi-line)
    pass=${pass//[^0-9]/}; pass=${pass:-0}
    fail=${fail//[^0-9]/}; fail=${fail:-0}

    TOTAL_PASS=$((TOTAL_PASS + pass))
    TOTAL_FAIL=$((TOTAL_FAIL + fail))

    if [ "$rc" -eq 0 ]; then
        echo -e "  ${GREEN}PASSED${NC} ($pass checks)"
        RESULTS+=("PASS  $scenario  ($pass passed, $fail failed)")
    else
        echo -e "  ${RED}FAILED${NC} ($fail failed, $pass passed)"
        RESULTS+=("FAIL  $scenario  ($pass passed, $fail failed)")
    fi
    echo ""
done

echo ""
echo -e "${BOLD}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   Final Report                                           ║${NC}"
echo -e "${BOLD}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

for result in "${RESULTS[@]}"; do
    if [[ "$result" == PASS* ]]; then
        echo -e "  ${GREEN}$result${NC}"
    elif [[ "$result" == FAIL* ]]; then
        echo -e "  ${RED}$result${NC}"
    else
        echo -e "  ${YELLOW}$result${NC}"
    fi
done

echo ""
echo -e "${BOLD}Total Assertions:${NC}"
echo -e "  ${GREEN}Passed:${NC} $TOTAL_PASS"
echo -e "  ${RED}Failed:${NC} $TOTAL_FAIL"
echo -e "  Total:  $((TOTAL_PASS + TOTAL_FAIL))"
echo ""

if [ "$TOTAL_FAIL" -gt 0 ]; then
    echo -e "${RED}${BOLD}SUITE FAILED${NC} — $TOTAL_FAIL assertion(s) failed"
    echo "  Logs: $LOG_DIR/"
    exit 1
else
    echo -e "${GREEN}${BOLD}ALL SCENARIOS PASSED${NC}"
    exit 0
fi
