# entrypoint-helpers.sh - Shared utilities for docker-entrypoint.sh
# Sourced by docker-entrypoint.sh

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# App paths (set by entrypoint before sourcing)
APP_DIR="${APP_DIR:-/app}"
SCRIPTS_DIR="${SCRIPTS_DIR:-/app/scripts}"

# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------
validate_port() {
    local port="$1"
    local name="${2:-port}"
    if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
        echo -e "${RED}Invalid ${name}: ${port}${NC}" >&2
        return 1
    fi
    return 0
}

ensure_dir() {
    local dir="$1"
    if [ -z "$dir" ]; then
        echo -e "${RED}ensure_dir: directory path is empty${NC}" >&2
        return 1
    fi
    mkdir -p "$dir" || { echo -e "${RED}Failed to create directory: ${dir}${NC}" >&2; return 1; }
    return 0
}

# -----------------------------------------------------------------------------
# Cleanup / signal handling
# -----------------------------------------------------------------------------
cleanup() {
    local sig="${1:-EXIT}"
    if [ -n "${SERVER_PID:-}" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        echo ""
        echo "Received $sig, shutting down Nexus server (PID $SERVER_PID)..."
        kill -TERM "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
    fi
    if [ -n "${ZOEKT_PID:-}" ] && kill -0 "$ZOEKT_PID" 2>/dev/null; then
        kill -TERM "$ZOEKT_PID" 2>/dev/null
        wait "$ZOEKT_PID" 2>/dev/null
    fi
    exit 0
}

# -----------------------------------------------------------------------------
# Safe run helpers
# -----------------------------------------------------------------------------
run_python() {
    local script="$1"
    shift
    if [ ! -f "$script" ]; then
        echo -e "${RED}Script not found: ${script}${NC}" >&2
        return 1
    fi
    python3 "$script" "$@" || return 1
    return 0
}
