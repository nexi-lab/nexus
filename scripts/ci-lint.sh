#!/usr/bin/env bash
# ci-lint.sh — Run the exact same lint checks as CI locally.
# Usage:
#   ./scripts/ci-lint.sh          # run all checks
#   ./scripts/ci-lint.sh lint     # ruff lint only
#   ./scripts/ci-lint.sh format   # ruff format only
#   ./scripts/ci-lint.sh mypy     # mypy only
#   ./scripts/ci-lint.sh quality  # code-quality checks (file-size, type-ignore, brick-imports)
#   ./scripts/ci-lint.sh test     # unit tests (same flags as CI)
#   ./scripts/ci-lint.sh fix      # auto-fix ruff lint + format

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

pass() { echo -e "${GREEN}✓ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; }
info() { echo -e "${YELLOW}→ $1${NC}"; }

run_ruff_lint() {
    info "ruff check (matches: uv run ruff check .)"
    if uv run ruff check .; then
        pass "ruff lint"
    else
        fail "ruff lint"
        return 1
    fi
}

run_ruff_format() {
    info "ruff format --check (matches: uv run ruff format --check .)"
    if uv run ruff format --check .; then
        pass "ruff format"
    else
        fail "ruff format"
        return 1
    fi
}

run_mypy() {
    info "mypy src/nexus (matches: uv run mypy src/nexus)"
    if uv run mypy src/nexus; then
        pass "mypy"
    else
        fail "mypy"
        return 1
    fi
}

run_file_size() {
    info "file size check (2000 line limit)"
    if python .pre-commit-hooks/check_file_size.py $(find src -name '*.py'); then
        pass "file size"
    else
        fail "file size"
        return 1
    fi
}

run_type_ignore() {
    info "type-ignore baseline check (max 569)"
    local count
    count=$(grep -r "# type: ignore" src/ --include="*.py" | wc -l | tr -d ' ')
    if [ "$count" -gt 569 ]; then
        fail "type-ignore baseline: $count > 569"
        return 1
    else
        pass "type-ignore baseline: $count / 569"
    fi
}

run_brick_imports() {
    info "brick import architecture check"
    if python .pre-commit-hooks/check_brick_imports.py; then
        pass "brick imports"
    else
        fail "brick imports"
        return 1
    fi
}

run_cli_theme() {
    info "CLI theme compliance check (no bare color tags)"
    if bash scripts/check_cli_theme.sh; then
        pass "CLI theme"
    else
        fail "CLI theme"
        return 1
    fi
}

run_fix() {
    info "auto-fixing: ruff check --fix + ruff format"
    uv run ruff check . --fix
    uv run ruff format .
    pass "auto-fix complete"
}

run_unit_tests() {
    info "unit tests (matches CI: uv run pytest tests/unit -v --durations=10)"
    if uv run pytest tests/unit -v --durations=10 -o "addopts="; then
        pass "unit tests"
    else
        fail "unit tests"
        return 1
    fi
}

run_all() {
    local failed=0
    run_ruff_lint   || failed=1
    run_ruff_format || failed=1
    run_mypy        || failed=1
    run_file_size   || failed=1
    run_type_ignore || failed=1
    run_brick_imports || failed=1
    run_cli_theme     || failed=1

    echo ""
    if [ "$failed" -eq 0 ]; then
        pass "All CI lint checks passed"
    else
        fail "Some checks failed — fix before pushing"
        return 1
    fi
}

case "${1:-all}" in
    lint)    run_ruff_lint ;;
    format)  run_ruff_format ;;
    mypy)    run_mypy ;;
    quality) run_file_size && run_type_ignore && run_brick_imports ;;
    test)    run_unit_tests ;;
    fix)     run_fix ;;
    all)     run_all ;;
    *)
        echo "Usage: $0 {all|lint|format|mypy|quality|test|fix}"
        exit 1
        ;;
esac
