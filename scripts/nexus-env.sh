#!/usr/bin/env bash
# Convenience script to set Nexus environment variables.
# Usage:  source scripts/nexus-env.sh
#         source scripts/nexus-env.sh --grpc-port 2121
#
# If .nexus-admin-env exists (created by `nexus serve --init`), it is sourced
# automatically.  You can override any value via flags or env vars.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# ── Defaults ──────────────────────────────────────────────────────────────────
_NEXUS_URL="${NEXUS_URL:-http://localhost:2120}"
_NEXUS_API_KEY="${NEXUS_API_KEY:-}"
_NEXUS_GRPC_PORT="${NEXUS_GRPC_PORT:-2121}"

# ── Source .nexus-admin-env if present ────────────────────────────────────────
ENV_FILE="${REPO_ROOT}/.nexus-admin-env"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    # .nexus-admin-env may set NEXUS_API_KEY, NEXUS_URL, NEXUS_GRPC_PORT;
    # keep those as new defaults but let CLI flags below override them.
    _NEXUS_URL="${NEXUS_URL:-$_NEXUS_URL}"
    _NEXUS_API_KEY="${NEXUS_API_KEY:-$_NEXUS_API_KEY}"
    _NEXUS_GRPC_PORT="${NEXUS_GRPC_PORT:-$_NEXUS_GRPC_PORT}"
fi

# ── Parse CLI flags ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --url)         _NEXUS_URL="$2";       shift 2 ;;
        --api-key)     _NEXUS_API_KEY="$2";   shift 2 ;;
        --grpc-port)   _NEXUS_GRPC_PORT="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: source scripts/nexus-env.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --url URL            Set NEXUS_URL         (default: http://localhost:2120)"
            echo "  --api-key KEY        Set NEXUS_API_KEY"
            echo "  --grpc-port PORT     Set NEXUS_GRPC_PORT   (default: 2121)"
            echo ""
            echo "Also sources .nexus-admin-env if it exists in the repo root."
            return 0 2>/dev/null || exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            return 1 2>/dev/null || exit 1
            ;;
    esac
done

# ── Export ────────────────────────────────────────────────────────────────────
export NEXUS_URL="$_NEXUS_URL"
export NEXUS_GRPC_PORT="$_NEXUS_GRPC_PORT"

if [[ -n "$_NEXUS_API_KEY" ]]; then
    export NEXUS_API_KEY="$_NEXUS_API_KEY"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo "Nexus environment configured:"
echo "  NEXUS_URL       = $NEXUS_URL"
echo "  NEXUS_GRPC_PORT = $NEXUS_GRPC_PORT"
if [[ -n "${NEXUS_API_KEY:-}" ]]; then
    # Show only last 8 chars of the key
    _masked="...${NEXUS_API_KEY: -8}"
    echo "  NEXUS_API_KEY   = ${_masked}"
else
    echo "  NEXUS_API_KEY   = (not set)"
fi
