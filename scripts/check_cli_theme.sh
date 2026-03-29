#!/usr/bin/env bash
# check_cli_theme.sh — Enforce CLI theme compliance (Issue #3241).
#
# Two checks:
#   1. No bare color tags in src/nexus/cli/ (all must use nexus.* tokens)
#   2. All [nexus.xxx] tokens reference one of the 12 defined semantic tokens
#
# Usage:
#   ./scripts/check_cli_theme.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FAILED=0

# -- Check 1: Bare color tags -------------------------------------------
# Match [red], [/red], [green], [/green], etc. but NOT inside theme.py itself.
# Also match style="red", style="green", etc.
BARE_TAGS=$(grep -rn \
    --include="*.py" \
    -E '\[(/?)(red|green|yellow|cyan|magenta|blue)\]|style="(red|green|yellow|cyan|magenta|blue)"' \
    "$ROOT/src/nexus/cli/" \
    | grep -v 'theme\.py' \
    | grep -v '# noqa: theme' \
    || true)

if [ -n "$BARE_TAGS" ]; then
    echo "ERROR: Bare color tags found in src/nexus/cli/ (use nexus.* theme tokens instead):"
    echo "$BARE_TAGS"
    FAILED=1
else
    echo "OK: No bare color tags"
fi

# -- Check 2: Valid nexus.* tokens --------------------------------------
# Extract all [nexus.xxx] references and verify they are in the allowed set.
VALID_TOKENS="nexus.success|nexus.warning|nexus.error|nexus.info|nexus.accent|nexus.muted|nexus.hint|nexus.path|nexus.value|nexus.label|nexus.identity|nexus.reference"

INVALID_TOKENS=$(grep -rn \
    --include="*.py" \
    -oE '\[/?nexus\.[a-z_.]+\]' \
    "$ROOT/src/nexus/cli/" \
    | grep -vE "\[/?($VALID_TOKENS)\]" \
    || true)

if [ -n "$INVALID_TOKENS" ]; then
    echo "ERROR: Invalid nexus.* tokens found (typo?):"
    echo "$INVALID_TOKENS"
    FAILED=1
else
    echo "OK: All nexus.* tokens are valid"
fi

# -- Check 3: Bare Console() instantiations -----------------------------
BARE_CONSOLE=$(grep -rn \
    --include="*.py" \
    'Console()' \
    "$ROOT/src/nexus/cli/" \
    | grep -v 'theme\.py' \
    | grep -v 'test_' \
    || true)

if [ -n "$BARE_CONSOLE" ]; then
    echo "ERROR: Bare Console() found (use 'from nexus.cli.theme import console' instead):"
    echo "$BARE_CONSOLE"
    FAILED=1
else
    echo "OK: No bare Console() instances"
fi

exit $FAILED
