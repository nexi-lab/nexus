#!/usr/bin/env python3
"""Migrate NexusFS syscall call sites from sync to async.

Transforms:
  1. `.sys_read(` → `await .sys_read(` (and other 10 syscalls + Tier 2)
  2. Containing `def func(` → `async def func(`
  3. Test functions get `@pytest.mark.asyncio` decorator

Usage:
  python scripts/migrate_async_syscalls.py [--dry-run] [--path src/]
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# Only sys_* methods — unique names, zero false positives.
# Tier 2 (read, write, append, edit, write_batch) are too generic
# (would match file.read(), response.write(), etc.) and handled separately.
SYSCALL_METHODS = {
    "sys_read",
    "sys_write",
    "sys_stat",
    "sys_setattr",
    "sys_unlink",
    "sys_rename",
    "mkdir",
    "rmdir",
    "sys_readdir",
    "access",
    "is_directory",
}

# Files to SKIP (definitions themselves, or non-Python)
SKIP_PATHS = {
    # filesystem_abc.py was deleted (NexusFilesystem Protocol removed)
    "scripts/migrate_async_syscalls.py",
}

# Pattern: `.method_name(` preceded by word char or `)` (object.method call)
# We need to be careful not to match:
#   - `def sys_read(` (method definitions)
#   - `"sys_read"` (string literals)
#   - `# sys_read(` (comments)
#   - `sys_read =` (assignments)
#   - standalone function calls like `read(` that aren't method calls


def _build_call_pattern() -> re.Pattern[str]:
    """Build regex matching `.syscall_method(` calls."""
    methods = "|".join(sorted(SYSCALL_METHODS))
    # Match: <object>.<method>( — where object access is via `.`
    # Negative lookbehind for `def ` and `async def ` to skip definitions
    # The `.` before method name distinguishes method calls from bare functions
    return re.compile(
        rf"(?<!\bdef )(?<!\basync def )"  # not a definition
        rf"(?<=\.)({methods})\s*\(",  # .method_name(
    )


CALL_PATTERN = _build_call_pattern()

# Pattern to find the `def` line that contains a given line
DEF_PATTERN = re.compile(r"^(\s*)(async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)

# Pattern for `@pytest.mark.asyncio`
ASYNCIO_MARKER = re.compile(r"@pytest\.mark\.asyncio")


def find_containing_def(lines: list[str], call_line_idx: int) -> int | None:
    """Find the `def` line index that contains the call at call_line_idx."""
    # Walk backwards to find the enclosing function definition
    call_indent = len(lines[call_line_idx]) - len(lines[call_line_idx].lstrip())
    for i in range(call_line_idx - 1, -1, -1):
        line = lines[i]
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or stripped.startswith("@"):
            continue
        m = DEF_PATTERN.match(line)
        if m:
            def_indent = len(m.group(1))
            if def_indent < call_indent:
                return i
    return None


def process_file(filepath: Path, dry_run: bool = False) -> dict[str, int]:
    """Process a single Python file. Returns change counts."""
    rel_path = str(filepath)
    for skip in SKIP_PATHS:
        if rel_path.endswith(skip):
            return {"skipped": 1}

    try:
        content = filepath.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return {"skipped": 1}

    # Quick check: does this file have any syscall calls?
    if not CALL_PATTERN.search(content):
        return {"no_matches": 1}

    lines = content.split("\n")
    changes = {"await_added": 0, "def_to_async": 0, "marker_added": 0}
    modified_defs: set[int] = set()  # track which def lines we've already made async

    # First pass: find all lines with syscall calls and add `await`
    is_test_file = "/tests/" in str(filepath) or str(filepath).startswith("tests/")

    for line_idx in range(len(lines)):
        line = lines[line_idx]

        # Skip comments and string-only lines
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue

        # Check for syscall method calls
        if not CALL_PATTERN.search(line):
            continue

        # Don't add await if already has await before the object
        # Find the start of the expression containing .sys_read(
        # We need to add `await` before the object, not before `.sys_read(`
        # e.g., `result = nx.sys_read(...)` → `result = nx.sys_read(...)`
        # e.g., `self._fs.sys_read(...)` → `self._fs.sys_read(...)`

        # Check if `await` is already present on this line before the call
        if "await " in line and CALL_PATTERN.search(
            line.split("await ")[-1] if "await " in line else line
        ):
            # await might already be there
            # More precise check: is there an await right before the object.method call?
            pass

        # Find where to insert `await`
        # Strategy: find the method call, walk backwards to find the start of the expression
        new_line = _add_await_to_line(line)
        if new_line != line:
            lines[line_idx] = new_line
            changes["await_added"] += 1

            # Make the containing function async
            def_idx = find_containing_def(lines, line_idx)
            if def_idx is not None and def_idx not in modified_defs:
                def_line = lines[def_idx]
                if "async def " not in def_line:
                    lines[def_idx] = def_line.replace("def ", "async def ", 1)
                    modified_defs.add(def_idx)
                    changes["def_to_async"] += 1

                    # Add @pytest.mark.asyncio for test functions
                    if is_test_file:
                        _add_asyncio_marker(lines, def_idx, changes)

    new_content = "\n".join(lines)

    # Add pytest import if we added markers and it's not already imported
    if changes["marker_added"] > 0 and "import pytest" not in new_content:
        # Add `import pytest` after the last import line
        new_content = _ensure_pytest_import(new_content)

    if new_content != content:
        if not dry_run:
            filepath.write_text(new_content, encoding="utf-8")
        return changes

    return {"no_changes": 1}


def _add_await_to_line(line: str) -> str:
    """Add `await` before object.syscall_method() calls in a line."""
    # Handle multiple calls on one line by processing right-to-left
    methods = "|".join(sorted(SYSCALL_METHODS))

    # Pattern: captures everything before `.method(`
    # We need to find the start of the expression chain
    # e.g., `result = self._fs.sys_read(path)` → `result = self._fs.sys_read(path)`
    # e.g., `nx.sys_write(p, d)` → `nx.sys_write(p, d)`
    # e.g., `return self.sys_stat(p)` → `return self.sys_stat(p)`
    # e.g., `if self.access(p):` → `if await self.access(p):`

    # Skip if already has await
    # Find all method call positions
    pattern = re.compile(rf"\.({methods})\s*\(")

    matches = list(pattern.finditer(line))
    if not matches:
        return line

    # Process right-to-left to preserve positions
    result = line
    for m in reversed(matches):
        call_start = m.start()  # position of the `.`

        # Walk backwards from `.` to find start of expression
        expr_start = _find_expression_start(result, call_start)

        # Check if already preceded by `await `
        prefix = result[:expr_start].rstrip()
        if prefix.endswith("await"):
            continue

        # Also skip if this is inside a string (basic check)
        before = result[:expr_start]
        if before.count("'") % 2 != 0 or before.count('"') % 2 != 0:
            continue

        # Insert `await `
        result = result[:expr_start] + "await " + result[expr_start:]

    return result


def _find_expression_start(line: str, dot_pos: int) -> int:
    """Find the start of the expression ending at dot_pos.

    e.g., for `result = self._fs.sys_read(`, dot_pos points to the `.` before
    sys_read. We want to find the start of `self._fs` (after `= `).
    """
    pos = dot_pos - 1
    depth = 0  # track parentheses/brackets

    while pos >= 0:
        ch = line[pos]
        if ch in ")]}":
            depth += 1
            pos -= 1
        elif ch in "([{":
            if depth > 0:
                depth -= 1
                pos -= 1
            else:
                break
        elif depth > 0:
            pos -= 1
        elif ch in " \t=,:;!&|^~<>+-%*/" or ch == "\\":
            break
        else:
            pos -= 1

    return pos + 1


def _add_asyncio_marker(lines: list[str], def_idx: int, changes: dict[str, int]) -> None:
    """Add @pytest.mark.asyncio above a test function if not already present."""
    # Check if there's already a marker
    check_idx = def_idx - 1
    while check_idx >= 0:
        check_line = lines[check_idx].strip()
        if check_line.startswith("@"):
            if "pytest.mark.asyncio" in check_line:
                return  # already has it
            check_idx -= 1
        elif check_line == "" or check_line.startswith("#"):
            check_idx -= 1
        else:
            break

    # Get the function name
    m = DEF_PATTERN.match(lines[def_idx])
    if not m:
        return
    func_name = m.group(3)

    # Only add marker to test functions (test_*) and fixtures
    if not func_name.startswith("test_"):
        return

    # Add marker with same indentation as def
    indent = len(lines[def_idx]) - len(lines[def_idx].lstrip())
    marker_line = " " * indent + "@pytest.mark.asyncio"
    lines.insert(def_idx, marker_line)
    changes["marker_added"] += 1


def _ensure_pytest_import(content: str) -> str:
    """Ensure `import pytest` is present."""
    if "import pytest" in content:
        return content
    # Add after last import
    lines = content.split("\n")
    last_import = 0
    for i, line in enumerate(lines):
        if line.startswith("import ") or line.startswith("from "):
            last_import = i
    lines.insert(last_import + 1, "import pytest")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate syscall calls to async")
    parser.add_argument("--dry-run", action="store_true", help="Don't write changes")
    parser.add_argument("--path", default=".", help="Root path to scan")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    root = Path(args.path)
    total = {"await_added": 0, "def_to_async": 0, "marker_added": 0, "files_changed": 0}

    py_files = sorted(root.rglob("*.py"))
    for filepath in py_files:
        # Skip non-source files
        rel = str(filepath)
        if "/__pycache__/" in rel or "/.git/" in rel or "/node_modules/" in rel:
            continue
        if "/migrations/" in rel:
            continue

        result = process_file(filepath, dry_run=args.dry_run)

        if result.get("await_added", 0) > 0 or result.get("def_to_async", 0) > 0:
            total["files_changed"] += 1
            total["await_added"] += result.get("await_added", 0)
            total["def_to_async"] += result.get("def_to_async", 0)
            total["marker_added"] += result.get("marker_added", 0)
            if args.verbose:
                print(
                    f"  {filepath}: +{result.get('await_added', 0)} await, "
                    f"+{result.get('def_to_async', 0)} async def, "
                    f"+{result.get('marker_added', 0)} markers"
                )

    action = "Would change" if args.dry_run else "Changed"
    print(f"\n{action}:")
    print(f"  Files:        {total['files_changed']}")
    print(f"  await added:  {total['await_added']}")
    print(f"  def→async:    {total['def_to_async']}")
    print(f"  markers:      {total['marker_added']}")


if __name__ == "__main__":
    main()
