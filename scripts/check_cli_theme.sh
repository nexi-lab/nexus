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
# Match bare color names in Rich markup — simple, composite, style attrs, and
# dict-style color values — but NOT nexus.* tokens and NOT theme.py itself.
#
# Catches:
#   Simple:    [red], [/red], [green], …
#   Composite: [bold red], [/bold red], [dim yellow], [italic cyan], …
#   Style attrs: style="red", style="bold cyan", style="dim green", …
#   Dict values: "pending": "yellow", "status": "green", …
#   Variable assigns: color = "red", status_style = "green", …
COLORS="red|green|yellow|cyan|magenta|blue|white"
MODIFIERS="bold|dim|italic|underline|strike|reverse|blink"
BARE_TAGS=$(grep -rn \
    --include="*.py" \
    -E "\[/?(($MODIFIERS) )?($COLORS)\]|style=\"(($MODIFIERS) )?($COLORS)\"|\":\s*\"($COLORS)\"|= \"($COLORS)\"" \
    "$ROOT/src/nexus/cli/" \
    | grep -v 'theme\.py' \
    | grep -v '# noqa: theme' \
    | grep -vE '\[/?nexus\.' \
    || true)

if [ -n "$BARE_TAGS" ]; then
    echo "ERROR: Bare color tags found in src/nexus/cli/ (use nexus.* theme tokens instead):"
    echo "$BARE_TAGS"
    FAILED=1
else
    echo "OK: No bare color tags"
fi

# -- Check 2: Valid nexus.* tokens --------------------------------------
# Extract all nexus.xxx references from BOTH bracket markup and style attrs,
# then verify each is in the allowed set.
#
# Contexts matched:
#   Bracket markup:  [nexus.xxx], [/nexus.xxx]
#   Style attributes: style="nexus.xxx", header_style="nexus.xxx", etc.
VALID_TOKENS="nexus.success|nexus.warning|nexus.error|nexus.info|nexus.accent|nexus.table_header|nexus.muted|nexus.hint|nexus.path|nexus.value|nexus.label|nexus.identity|nexus.reference"

# Pass 1: bracket markup  — [nexus.xxx] and [/nexus.xxx]
INVALID_BRACKET=$(grep -rn \
    --include="*.py" \
    -oE '\[/?nexus\.[a-z_.]+\]' \
    "$ROOT/src/nexus/cli/" \
    | grep -vE "\[/?($VALID_TOKENS)\]" \
    || true)

# Pass 2: style attributes — style="nexus.xxx", header_style="nexus.xxx", etc.
INVALID_STYLE=$(grep -rn \
    --include="*.py" \
    -oE '[a-z_]*style="nexus\.[a-z_.]+"' \
    "$ROOT/src/nexus/cli/" \
    | grep -vE "\"($VALID_TOKENS)\"" \
    || true)

INVALID_TOKENS=""
[ -n "$INVALID_BRACKET" ] && INVALID_TOKENS="$INVALID_BRACKET"
if [ -n "$INVALID_STYLE" ]; then
    [ -n "$INVALID_TOKENS" ] && INVALID_TOKENS="$INVALID_TOKENS"$'\n'"$INVALID_STYLE" || INVALID_TOKENS="$INVALID_STYLE"
fi

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
