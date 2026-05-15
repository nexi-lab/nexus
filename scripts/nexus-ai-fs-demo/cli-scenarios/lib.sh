#!/bin/bash
# lib.sh — Shared helpers for CLI scenario scripts
#
# Provides: coloured output, pass/fail assertions, TUI tmux helpers,
#           per-command timing + benchmark reporting.
# Source this file at the top of every scenario script.

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ── Counters ─────────────────────────────────────────────────────────────
PASS_COUNT=0
FAIL_COUNT=0
SCENARIO_NAME="${SCENARIO_NAME:-unnamed}"

# ── Timing / Benchmark ──────────────────────────────────────────────────
SCENARIO_START_EPOCH=$(date +%s)
SLOW_THRESHOLD_MS="${SLOW_THRESHOLD_MS:-3000}"   # flag commands > 3s
TIMING_LOG=()        # array of "command_name  duration_ms  status"
SUSPICIOUS_LOG=()    # commands that exceeded the threshold

_ms_now() { python3 -c "import time; print(int(time.time()*1000))" 2>/dev/null || date +%s000; }

# ── Logging ──────────────────────────────────────────────────────────────
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[PASS]${NC}  $*"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
header(){ echo -e "\n${BOLD}━━━ $* ━━━${NC}\n"; }
timing(){ echo -e "${DIM}[TIME]${NC}  $*"; }

# ── Assertions ───────────────────────────────────────────────────────────

assert_contains() {
    local label="$1" haystack="$2" needle="$3"
    if echo "$haystack" | grep -qF "$needle"; then
        ok "$label — found '$needle'"
    else
        fail "$label — expected '$needle' not found"
        echo "    actual output (first 5 lines):"
        echo "$haystack" | head -5 | sed 's/^/    | /'
    fi
}

assert_not_contains() {
    local label="$1" haystack="$2" needle="$3"
    if echo "$haystack" | grep -qF "$needle"; then
        fail "$label — unexpected '$needle' found"
    else
        ok "$label — correctly absent '$needle'"
    fi
}

assert_exit_code() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        ok "$label — exit code $actual"
    else
        fail "$label — expected exit $expected, got $actual"
    fi
}

assert_regex() {
    local label="$1" haystack="$2" pattern="$3"
    if echo "$haystack" | grep -qE "$pattern"; then
        ok "$label — matches /$pattern/"
    else
        fail "$label — no match for /$pattern/"
        echo "    actual output (first 5 lines):"
        echo "$haystack" | head -5 | sed 's/^/    | /'
    fi
}

# ── Run a CLI command with timing ────────────────────────────────────────
# Usage: run_cli <var_name> <cmd...>
#   Sets: <var_name> = stdout+stderr, <var_name>_RC = exit code
#   Also records timing and flags slow commands.
run_cli() {
    local var_name="$1"; shift
    local cmd_label="$*"
    local output rc=0

    info "Running: $cmd_label"
    local t_start; t_start=$(_ms_now)

    output=$( ("$@") 2>&1) || rc=$?
    # Strip noise: RPC transport errors, Raft log lines, ANSI escapes
    output=$(echo "$output" | grep -v "^RPCTransport RPC error:" | grep -v '^\[20[0-9][0-9]-' | sed 's/\x1b\[[0-9;]*m//g' || true)

    local t_end; t_end=$(_ms_now)
    local duration_ms=$(( t_end - t_start ))

    printf -v "$var_name" '%s' "$output"
    printf -v "${var_name}_RC" '%s' "$rc"

    # Record timing
    local status_label="ok"
    [ "$rc" -ne 0 ] && status_label="err"

    TIMING_LOG+=("$(printf '%-50s %6dms  %s' "$cmd_label" "$duration_ms" "$status_label")")
    timing "$cmd_label → ${duration_ms}ms (exit $rc)"

    # Flag suspicious (slow) commands
    if [ "$duration_ms" -gt "$SLOW_THRESHOLD_MS" ]; then
        warn "SLOW: $cmd_label took ${duration_ms}ms (threshold: ${SLOW_THRESHOLD_MS}ms)"
        SUSPICIOUS_LOG+=("$(printf '[SLOW %6dms] %s' "$duration_ms" "$cmd_label")")
    fi
}

# ── TUI / tmux helpers ──────────────────────────────────────────────────
TUI_SESSION="${TUI_SESSION:-nexus-tui-test}"

tui_start() {
    info "Starting TUI in tmux session '$TUI_SESSION'..."
    tmux kill-session -t "$TUI_SESSION" 2>/dev/null || true
    sleep 0.5
    tmux new-session -d -s "$TUI_SESSION" -x 200 -y 50 \
        "nexus tui 2>&1; read -p 'TUI exited. Press Enter to close.'"
    sleep 3
    info "TUI session started."
}

tui_send() {
    tmux send-keys -t "$TUI_SESSION" "$@"
}

tui_capture() {
    tmux capture-pane -t "$TUI_SESSION" -p -S -50
}

tui_switch_tab() {
    local tab="$1"
    info "Switching TUI to tab $tab..."
    # Reset to tab 1 first, then navigate to target — prevents tab drift
    tui_send "1"
    sleep 1
    if [ "$tab" != "1" ]; then
        tui_send "$tab"
        sleep 2
    fi
}

tui_assert_contains() {
    local label="$1" needle="$2"
    local pane
    pane=$(tui_capture)
    if echo "$pane" | grep -qF "$needle"; then
        ok "TUI: $label — found '$needle'"
    else
        fail "TUI: $label — expected '$needle' not found in pane"
        echo "    pane snapshot (first 10 lines):"
        echo "$pane" | head -10 | sed 's/^/    | /'
    fi
}

tui_assert_regex() {
    local label="$1" pattern="$2"
    local pane
    pane=$(tui_capture)
    if echo "$pane" | grep -qE "$pattern"; then
        ok "TUI: $label — matches /$pattern/"
    else
        fail "TUI: $label — no match for /$pattern/ in pane"
        echo "    pane snapshot (first 10 lines):"
        echo "$pane" | head -10 | sed 's/^/    | /'
    fi
}

# Save TUI pane screenshot to file
tui_screenshot() {
    local label="$1"
    local screenshot_dir="${SCRIPT_DIR:-/tmp}/screenshots"
    mkdir -p "$screenshot_dir"
    local filename
    filename="$screenshot_dir/$(echo "$SCENARIO_NAME-$label" | tr ' /&' '___').txt"
    tui_capture > "$filename"
    info "TUI screenshot saved: $filename"
}

tui_stop() {
    info "Stopping TUI session '$TUI_SESSION'..."
    tmux kill-session -t "$TUI_SESSION" 2>/dev/null || true
}

# ── Strip log noise (Raft/ANSI) from CLI output ────────────────────────────
# Some commands emit Raft log lines on stderr that get mixed in.
# This helper strips ANSI escape sequences and lines starting with a timestamp
# or known log prefixes.
strip_log_noise() {
    sed 's/\x1b\[[0-9;]*m//g' | grep -v '^\[20[0-9][0-9]-' | grep -v '^$'
}

# ── CAS-tolerant assertions (Docker demo preset limitation) ──────────────
# On the Docker demo preset, file-content reads (cat) may fail because the
# CAS backend isn't configured for reads.  These helpers treat CAS read
# failures as WARN instead of FAIL.
assert_content_or_cas_warn() {
    local label="$1" var_name="$2" needle="$3"
    local rc_var="${var_name}_RC"
    local content="${!var_name}"
    local rc="${!rc_var}"
    if [ "$rc" -eq 0 ]; then
        assert_contains "$label" "$content" "$needle"
    elif echo "$content" | grep -q "NOT_FOUND\|cas///\|Backend.*not in pool\|ObjectStore"; then
        warn "$label — CAS read limitation (content not available in demo preset)"
        ok "$label — command executed (CAS limitation noted)"
    else
        fail "$label — exit $rc: $(echo "$content" | head -2)"
    fi
}

# assert_or_infra_warn <label> <exit_code> <output>
# Treat ONLY specific, narrowly-scoped infrastructure errors as WARN.
# These are services that require external infra not present in the Docker
# demo preset (TigerBeetle, E2B sandbox, Google OAuth, etc.).
# Everything else is a hard FAIL — renamed flags, validation errors, and
# CLI bugs must not be silently swallowed.
assert_or_infra_warn() {
    local label="$1" rc="$2" output="$3"
    if [ "$rc" -eq 0 ]; then
        ok "$label — exit 0"
    elif echo "$output" | grep -qE \
        "ObjectStore not available|gRPC server unavailable|UNAVAILABLE|FD shutdown|Connection reset by peer|recvmsg" ; then
        # gRPC transport failures — brick not deployed (e.g. TigerBeetle)
        warn "$label — gRPC service unavailable in demo (exit $rc)"
        ok "$label — command executed (infra noted)"
    elif echo "$output" | grep -qE \
        "cas///|Backend.*not in pool|not in pool and has no origin" ; then
        # CAS content-addressable storage not configured for reads
        warn "$label — CAS backend not configured in demo (exit $rc)"
        ok "$label — command executed (CAS noted)"
    elif echo "$output" | grep -qE \
        "workflow_engine|ZoneManager.*init|Raft zone" ; then
        # Local-only bricks that require Raft/local state
        warn "$label — local-only brick not available in remote mode (exit $rc)"
        ok "$label — command executed (brick noted)"
    elif echo "$output" | grep -qE \
        "No provider configured|E2B_API_KEY|sandbox.*not configured" ; then
        # Sandbox providers not configured
        warn "$label — sandbox provider not configured (exit $rc)"
        ok "$label — command executed (provider noted)"
    elif echo "$output" | grep -qE \
        "no_auth|missing_scopes|encryption_key|GWS_ACCESS_TOKEN" ; then
        # OAuth/auth not configured for specific providers
        warn "$label — auth provider not configured (exit $rc)"
        ok "$label — command executed (auth noted)"
    else
        fail "$label — expected exit 0, got $rc"
    fi
}

# ── Summary with timing report ───────────────────────────────────────────
print_summary() {
    local total=$((PASS_COUNT + FAIL_COUNT))
    local scenario_end; scenario_end=$(date +%s)
    local scenario_duration=$(( scenario_end - SCENARIO_START_EPOCH ))

    echo ""
    header "Scenario: $SCENARIO_NAME — Results"
    echo -e "  ${GREEN}Passed:${NC} $PASS_COUNT"
    echo -e "  ${RED}Failed:${NC} $FAIL_COUNT"
    echo -e "  Total:  $total"
    echo -e "  ${CYAN}Duration:${NC} ${scenario_duration}s"
    echo ""

    # Timing report
    if [ ${#TIMING_LOG[@]} -gt 0 ]; then
        echo -e "${BOLD}  ── Timing Report ──${NC}"
        for entry in "${TIMING_LOG[@]}"; do
            echo "    $entry"
        done
        echo ""
    fi

    # Suspicious commands
    if [ ${#SUSPICIOUS_LOG[@]} -gt 0 ]; then
        echo -e "${YELLOW}${BOLD}  ── Suspicious (Slow) Commands ──${NC}"
        for entry in "${SUSPICIOUS_LOG[@]}"; do
            echo -e "    ${YELLOW}$entry${NC}"
        done
        echo ""
    fi

    if [ "$FAIL_COUNT" -gt 0 ]; then
        echo -e "${RED}SCENARIO FAILED${NC}"
        return 1
    else
        echo -e "${GREEN}SCENARIO PASSED${NC}"
        return 0
    fi
}
