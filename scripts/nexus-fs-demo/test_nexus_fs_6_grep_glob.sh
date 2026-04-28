#!/usr/bin/env bash
# =============================================================================
# Script 6: Grep & Glob (Correctness + Performance)
# =============================================================================
# Tests: nexus-fs grep / nexus-fs glob CLI commands and Python API, including
#        Rust-accelerated paths vs Python fallback performance.
#
# Prereq: Run script 1 first (creates /tmp/nexus-fs-demo and mounts)
#         Run script 2 first (seeds files via nexus-fs write)
#
# =============================================================================
set -euo pipefail

PYTHON="${NEXUS_FS_PYTHON:-/Users/tafeng/nexus/.venv/bin/python}"
TESTROOT="/tmp/nexus-fs-demo"
nfs() { "$PYTHON" -c "from nexus.fs._cli import main; main()" "$@"; }
# Force human output even in non-TTY subshells ($(...))
export NEXUS_NO_AUTO_JSON=1

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
MAGENTA='\033[0;35m'; NC='\033[0m'
step()   { echo -e "\n${CYAN}[$1]${NC} $2"; }
ok()     { echo -e "  ${GREEN}OK${NC} $1"; }
fail()   { echo -e "  ${RED}FAIL${NC} $1"; FAILURES=$((FAILURES+1)); }
banner() { echo -e "\n${YELLOW}════════════════════════════════════════════════${NC}"; echo -e "${YELLOW}  $1${NC}"; echo -e "${YELLOW}════════════════════════════════════════════════${NC}"; }
perf()   { echo -e "  ${MAGENTA}PERF${NC} $1"; }

FAILURES=0
TOTAL=0
pass() { TOTAL=$((TOTAL+1)); ok "$1"; }
check_fail() { TOTAL=$((TOTAL+1)); fail "$1"; }

banner "Script 6: Grep & Glob"

# ── Verify prereqs ───────────────────────────────────────────────────────────
if [ ! -d "$TESTROOT/projects" ] || [ ! -d "$TESTROOT/datasets" ]; then
    echo -e "${RED}Run scripts 1 and 2 first to create test directories and seed files${NC}"
    exit 1
fi

# ── Step 1: Seed additional files for rich search coverage ──────────────────
step "1/14" "Seeding additional test files for search coverage..."
mkdir -p "$TESTROOT/projects/tests" "$TESTROOT/projects/lib"

cat > "$TESTROOT/projects/tests/test_main.py" << 'PYEOF'
import pytest
from src.main import hello

def test_hello():
    assert hello() == "Hello from nexus-fs!"

def test_hello_type():
    assert isinstance(hello(), str)

# TODO: add integration tests
PYEOF

cat > "$TESTROOT/projects/lib/helpers.py" << 'PYEOF'
"""Helper utilities for the demo project."""
import os
import sys
import json

def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

def load_json(path: str) -> dict:
    with open(path) as f:
        return json.load(f)

class Config:
    """Simple configuration loader."""
    def __init__(self, path: str):
        self.data = load_json(path)

    def get(self, key: str) -> str:
        return self.data.get(key, "")
PYEOF

cat > "$TESTROOT/projects/docs/api.md" << 'EOF'
# API Reference

## Functions

### hello()
Returns a greeting string.

### load_config(path)
Loads a JSON configuration file.

## Error Handling
All functions raise ValueError on invalid input.
EOF

cat > "$TESTROOT/datasets/schema.sql" << 'EOF'
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE,
    role TEXT DEFAULT 'viewer'
);

CREATE INDEX idx_users_email ON users(email);

-- TODO: add audit log table
INSERT INTO users (name, email, role) VALUES ('admin', 'admin@example.com', 'admin');
EOF

# Seed into CAS
nfs write /local/nexus-fs-demo-projects/tests/test_main.py < "$TESTROOT/projects/tests/test_main.py"
nfs write /local/nexus-fs-demo-projects/lib/helpers.py < "$TESTROOT/projects/lib/helpers.py"
nfs write /local/nexus-fs-demo-projects/docs/api.md < "$TESTROOT/projects/docs/api.md"
nfs write /local/nexus-fs-demo-datasets/schema.sql < "$TESTROOT/datasets/schema.sql"
ok "Seeded 4 additional files"

# ═══════════════════════════════════════════════════════════════════════════
#  GREP: Correctness
# ═══════════════════════════════════════════════════════════════════════════

step "2/14" "grep: basic literal pattern..."
OUTPUT=$(nfs grep "def hello" /local/nexus-fs-demo-projects/ 2>&1)
if echo "$OUTPUT" | grep -q "main.py.*def hello"; then
    pass "grep 'def hello' found match in main.py"
else
    check_fail "grep 'def hello' should match main.py"
    echo "    output: $(echo "$OUTPUT" | head -3)"
fi

step "3/14" "grep: regex pattern..."
OUTPUT=$(nfs grep "def [a-z_]+\(" /local/nexus-fs-demo-projects/ 2>&1)
MATCH_COUNT=$(echo "$OUTPUT" | grep -c "match(es) found" | tr -d ' ' || true)
# Should find: hello, test_hello, test_hello_type, get_env, load_json, load_config, get
FUNC_LINES=$(echo "$OUTPUT" | grep -c ":.*def " || true)
if [ "$FUNC_LINES" -ge 5 ]; then
    pass "grep regex found $FUNC_LINES function definitions"
else
    check_fail "grep regex expected >=5 function defs, got $FUNC_LINES"
    echo "    output: $(echo "$OUTPUT" | head -10)"
fi

step "4/14" "grep: case-insensitive (-i)..."
OUTPUT=$(nfs grep "todo" /local/nexus-fs-demo-projects/ -i 2>&1)
TODO_LINES=$(echo "$OUTPUT" | grep -c "TODO\|todo" || true)
if [ "$TODO_LINES" -ge 1 ]; then
    pass "grep -i 'todo' found $TODO_LINES TODO comments"
else
    check_fail "grep -i 'todo' should find TODO comments"
    echo "    output: $(echo "$OUTPUT" | head -5)"
fi

step "5/14" "grep: max results (-n)..."
OUTPUT=$(nfs grep "import" /local/nexus-fs-demo-projects/ -n 2 2>&1)
IMPORT_LINES=$(echo "$OUTPUT" | grep ":.*import" | wc -l | tr -d ' ')
if [ "$IMPORT_LINES" -le 2 ]; then
    pass "grep -n 2 returned $IMPORT_LINES results (capped at 2)"
else
    check_fail "grep -n 2 should cap at 2 results, got $IMPORT_LINES"
fi

step "6/14" "grep: cross-backend (datasets)..."
OUTPUT=$(nfs grep "admin" /local/nexus-fs-demo-datasets/ 2>&1)
if echo "$OUTPUT" | grep -q "admin"; then
    pass "grep 'admin' found matches in datasets"
else
    check_fail "grep 'admin' should match in datasets (users.csv, schema.sql)"
    echo "    output: $(echo "$OUTPUT" | head -5)"
fi

step "7/14" "grep: JSON output..."
OUTPUT=$(NEXUS_NO_AUTO_JSON= nfs grep "def hello" /local/nexus-fs-demo-projects/ --json 2>&1)
if echo "$OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['data']['count']>=1; assert 'matches' in d['data']" 2>/dev/null; then
    pass "grep --json returns valid JSON with count and matches"
else
    check_fail "grep --json output is not valid JSON"
    echo "    output: $(echo "$OUTPUT" | head -3)"
fi

step "8/14" "grep: no matches returns zero..."
OUTPUT=$(nfs grep "ZZZZNOTFOUND999" /local/nexus-fs-demo-projects/ 2>&1)
if echo "$OUTPUT" | grep -q "0 match"; then
    pass "grep with no matches reports 0"
else
    check_fail "grep with no matches should report 0"
    echo "    output: $(echo "$OUTPUT" | head -3)"
fi

# ═══════════════════════════════════════════════════════════════════════════
#  GLOB: Correctness
# ═══════════════════════════════════════════════════════════════════════════

step "9/14" "glob: recursive pattern (**/*.py)..."
OUTPUT=$(nfs glob "**/*.py" /local/nexus-fs-demo-projects/ 2>&1)
PY_COUNT=$(echo "$OUTPUT" | grep "\.py$" | wc -l | tr -d ' ')
if [ "$PY_COUNT" -ge 3 ]; then
    pass "glob '**/*.py' found $PY_COUNT Python files"
else
    check_fail "glob '**/*.py' expected >=3 files, got $PY_COUNT"
    echo "    output: $(echo "$OUTPUT" | head -10)"
fi

step "10/14" "glob: extension filter (*.csv)..."
OUTPUT=$(nfs glob "*.csv" /local/nexus-fs-demo-datasets/ 2>&1)
CSV_COUNT=$(echo "$OUTPUT" | grep "\.csv$" | wc -l | tr -d ' ')
if [ "$CSV_COUNT" -ge 2 ]; then
    pass "glob '*.csv' found $CSV_COUNT CSV files"
else
    check_fail "glob '*.csv' expected >=2 CSV files, got $CSV_COUNT"
    echo "    output: $(echo "$OUTPUT" | head -10)"
fi

step "11/14" "glob: markdown files (**/*.md)..."
OUTPUT=$(nfs glob "**/*.md" /local/nexus-fs-demo-projects/ 2>&1)
MD_COUNT=$(echo "$OUTPUT" | grep "\.md$" | wc -l | tr -d ' ')
if [ "$MD_COUNT" -ge 2 ]; then
    pass "glob '**/*.md' found $MD_COUNT markdown files"
else
    check_fail "glob '**/*.md' expected >=2 markdown files, got $MD_COUNT"
    echo "    output: $(echo "$OUTPUT" | head -10)"
fi

step "12/14" "glob: JSON output..."
OUTPUT=$(NEXUS_NO_AUTO_JSON= nfs glob "**/*.py" /local/nexus-fs-demo-projects/ --json 2>&1)
if echo "$OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['data']['count']>=3; assert 'matches' in d['data']" 2>/dev/null; then
    pass "glob --json returns valid JSON with count and matches"
else
    check_fail "glob --json output is not valid JSON"
    echo "    output: $(echo "$OUTPUT" | head -3)"
fi

step "13/14" "glob: no matches returns zero..."
OUTPUT=$(nfs glob "*.zzzzz" /local/nexus-fs-demo-datasets/ 2>&1)
if echo "$OUTPUT" | grep -q "0 file"; then
    pass "glob with no matches reports 0"
else
    check_fail "glob with no matches should report 0"
    echo "    output: $(echo "$OUTPUT" | head -3)"
fi

# ═══════════════════════════════════════════════════════════════════════════
#  PERFORMANCE: Rust vs Python fallback
# ═══════════════════════════════════════════════════════════════════════════

step "14/14" "Performance: Rust vs Python fallback (grep + glob)..."

"$PYTHON" << 'PYEOF'
import asyncio
import time
import re
import fnmatch
import sys

ITERATIONS = 50

async def main():
    import nexus.fs
    from nexus.fs._facade import _SLIM_CONTEXT

    # Boot filesystem with existing mounts
    from nexus.fs._paths import build_mount_args, load_persisted_mounts
    persisted = load_persisted_mounts()
    uris, overrides = build_mount_args(persisted)
    fs = await nexus.fs.mount(*uris, mount_overrides=overrides or None, skip_unavailable=True)

    # Collect file contents from projects backend for benchmarking
    entries = await fs._kernel.sys_readdir(
        "/local/nexus-fs-demo-projects/", recursive=True, details=True,
        context=_SLIM_CONTEXT,
    )
    file_paths = [
        e["path"] for e in entries
        if isinstance(e, dict) and not e.get("is_directory", False)
    ]
    file_contents = {}
    for fp in file_paths:
        try:
            file_contents[fp] = await fs._kernel.sys_read(fp, context=_SLIM_CONTEXT)
        except Exception:
            pass
    all_paths = list(file_contents.keys())

    if not file_contents:
        print("  SKIP  No files found for benchmarking")
        await fs.close()
        return

    print(f"  Benchmarking with {len(file_contents)} files, {ITERATIONS} iterations each")
    print()

    # ── Check Rust availability ──────────────────────────────────────────
    rust_grep_available = False
    rust_glob_available = False
    try:
        from nexus_runtime import grep_bulk as _rust_grep
        rust_grep_available = True
    except ImportError:
        _rust_grep = None
    try:
        from nexus_runtime import glob_match_bulk as _rust_glob
        rust_glob_available = True
    except ImportError:
        _rust_glob = None

    # ── GREP benchmark ───────────────────────────────────────────────────
    pattern = r"def [a-z_]+\("

    # Python fallback
    compiled = re.compile(pattern)
    t0 = time.perf_counter()
    for _ in range(ITERATIONS):
        py_matches = []
        for fp, content in file_contents.items():
            text = content.decode("utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), 1):
                m = compiled.search(line)
                if m:
                    py_matches.append({"file": fp, "line": line_no, "content": line, "match": m.group(0)})
    py_grep_ms = (time.perf_counter() - t0) / ITERATIONS * 1000

    # Rust accelerated
    if rust_grep_available:
        t0 = time.perf_counter()
        for _ in range(ITERATIONS):
            rust_matches = _rust_grep(pattern, file_contents, False, 1000)
        rust_grep_ms = (time.perf_counter() - t0) / ITERATIONS * 1000
        # Verify same results
        py_set = {(m["file"], m["line"]) for m in py_matches}
        rust_set = {(m["file"], m["line"]) for m in rust_matches}
        if py_set == rust_set:
            correctness = "MATCH"
        else:
            correctness = f"MISMATCH (py={len(py_set)}, rust={len(rust_set)})"
        speedup = py_grep_ms / rust_grep_ms if rust_grep_ms > 0 else float("inf")
        print(f"  GREP  Python: {py_grep_ms:.3f} ms | Rust: {rust_grep_ms:.3f} ms | Speedup: {speedup:.1f}x | {correctness}")
    else:
        print(f"  GREP  Python: {py_grep_ms:.3f} ms | Rust: N/A (not installed)")

    # ── GLOB benchmark ───────────────────────────────────────────────────
    glob_patterns = ["**/*.py"]

    # Python fallback
    t0 = time.perf_counter()
    for _ in range(ITERATIONS):
        py_glob = [p for p in all_paths if fnmatch.fnmatch(p, glob_patterns[0])]
    py_glob_ms = (time.perf_counter() - t0) / ITERATIONS * 1000

    # Rust accelerated
    if rust_glob_available:
        t0 = time.perf_counter()
        for _ in range(ITERATIONS):
            rust_glob = _rust_glob(glob_patterns, all_paths)
        rust_glob_ms = (time.perf_counter() - t0) / ITERATIONS * 1000
        # Verify same results
        if set(py_glob) == set(rust_glob):
            correctness = "MATCH"
        else:
            correctness = f"MISMATCH (py={len(py_glob)}, rust={len(rust_glob)})"
        speedup = py_glob_ms / rust_glob_ms if rust_glob_ms > 0 else float("inf")
        print(f"  GLOB  Python: {py_glob_ms:.3f} ms | Rust: {rust_glob_ms:.3f} ms | Speedup: {speedup:.1f}x | {correctness}")
    else:
        print(f"  GLOB  Python: {py_glob_ms:.3f} ms | Rust: N/A (not installed)")

    # ── End-to-end facade benchmark ──────────────────────────────────────
    print()
    e2e_iters = 20

    t0 = time.perf_counter()
    for _ in range(e2e_iters):
        await fs.grep(r"def [a-z_]+\(", "/local/nexus-fs-demo-projects/")
    e2e_grep_ms = (time.perf_counter() - t0) / e2e_iters * 1000
    print(f"  E2E   grep (facade, {e2e_iters} iters): {e2e_grep_ms:.1f} ms/call")

    t0 = time.perf_counter()
    for _ in range(e2e_iters):
        await fs.glob("**/*.py", "/local/nexus-fs-demo-projects/")
    e2e_glob_ms = (time.perf_counter() - t0) / e2e_iters * 1000
    print(f"  E2E   glob (facade, {e2e_iters} iters): {e2e_glob_ms:.1f} ms/call")

    await fs.close()

asyncio.run(main())
PYEOF
ok "Performance benchmark complete"

# ═══════════════════════════════════════════════════════════════════════════
#  Summary
# ═══════════════════════════════════════════════════════════════════════════

echo ""
banner "Grep & Glob Complete!"
echo ""
echo "  Results: $((TOTAL - FAILURES))/$TOTAL passed"
if [ "$FAILURES" -gt 0 ]; then
    echo -e "  ${RED}$FAILURES test(s) failed${NC}"
    exit 1
fi
echo ""
echo "  Correctness tests:"
echo "    - grep: literal, regex, case-insensitive, max-results, cross-backend, JSON, no-match"
echo "    - glob: recursive (**/*.py), extension (*.csv), markdown, JSON, no-match"
echo ""
echo "  Performance tests:"
echo "    - Rust vs Python grep (regex matching)"
echo "    - Rust vs Python glob (pattern filtering)"
echo "    - End-to-end facade latency (grep + glob)"
echo ""
