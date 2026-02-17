"""Fix TYPE_CHECKING imports used at runtime.

`if TYPE_CHECKING:` that appears in a runtime annotation (function signature,
class body variable annotation, default value) will cause a NameError.

This script finds such names and moves their imports from TYPE_CHECKING to
the regular import section.

Usage:
    python scripts/fix_tc_imports.py [--dry-run]
"""

import ast
import sys
from pathlib import Path

def get_tc_block_ranges(tree: ast.Module) -> list[tuple[int, int]]:
    """Get line ranges of TYPE_CHECKING if-blocks."""
    ranges = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                start = node.lineno
                end = max(
                    getattr(child, 'end_lineno', start)
                    for child in ast.walk(node)
                    if hasattr(child, 'end_lineno')
                )
                ranges.append((start, end))
    return ranges

def in_tc_block(lineno: int, tc_ranges: list[tuple[int, int]]) -> bool:
    """Check if a line number is inside a TYPE_CHECKING block."""
    return any(start <= lineno <= end for start, end in tc_ranges)

def in_string_annotation(node, lines: list[str]) -> bool:
    """Check if an AST node is inside a string annotation (already quoted)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    return False

def collect_tc_imports(tree: ast.Module) -> dict[str, tuple[str, str, int]]:
    """Collect all names imported under TYPE_CHECKING.

    Returns {imported_name: (module, original_name, lineno)}.
    """
    tc_names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                for stmt in node.body:
                    if isinstance(stmt, ast.ImportFrom) and stmt.module:
                        for alias in stmt.names:
                            imported_name = alias.asname or alias.name
                            tc_names[imported_name] = (
                                stmt.module,
                                alias.name,
                                stmt.lineno,
                            )
    return tc_names

def collect_runtime_name_usages(tree: ast.Module, tc_ranges: list[tuple[int, int]]) -> set[str]:
    """Collect all Name references that appear outside TYPE_CHECKING blocks."""
    used = set()

    class NameCollector(ast.NodeVisitor):
        def visit_Name(self, node):
            if not in_tc_block(node.lineno, tc_ranges):
                used.add(node.id)
            self.generic_visit(node)

        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name):
                if not in_tc_block(node.value.lineno, tc_ranges):
                    used.add(node.value.id)
            self.generic_visit(node)

    NameCollector().visit(tree)
    return used

def already_imported_at_runtime(tree: ast.Module, name: str, tc_ranges: list[tuple[int, int]]) -> bool:
    """Check if a name is already imported at module level (not in TC block)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if in_tc_block(node.lineno, tc_ranges):
                continue
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    return True
        elif isinstance(node, ast.Import):
            if in_tc_block(node.lineno, tc_ranges):
                continue
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    return True
    return False

def find_insert_point(lines: list[str], tree: ast.Module) -> int:
    """Find the best line to insert new runtime imports.

    Insert after the last existing `from nexus.*` import (or after other imports).
    """
    last_import_line = 0
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            end_line = getattr(node, 'end_lineno', node.lineno)
            last_import_line = max(last_import_line, end_line)
        elif isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                # Insert before TC block
                return node.lineno - 1
    return last_import_line

def fix_file(filepath: Path, dry_run: bool = False) -> int:
    """Fix TYPE_CHECKING imports used at runtime.

    Returns number of imports moved.
    """
    source = filepath.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0

    tc_ranges = get_tc_block_ranges(tree)
    if not tc_ranges:
        return 0

    tc_imports = collect_tc_imports(tree)
    if not tc_imports:
        return 0

    runtime_names = collect_runtime_name_usages(tree, tc_ranges)

    # Find TC names used at runtime that aren't already imported
    to_move = {}
    for name, (module, orig_name, lineno) in tc_imports.items():
        if name in runtime_names and not already_imported_at_runtime(tree, name, tc_ranges):
            to_move[name] = (module, orig_name)

    if not to_move:
        return 0

    # Group by module
    by_module: dict[str, list[tuple[str, str]]] = {}
    for name, (module, orig_name) in to_move.items():
        if module not in by_module:
            by_module[module] = []
        by_module[module].append((orig_name, name))

    # Build import lines
    new_lines = []
    for module in sorted(by_module.keys()):
        names = by_module[module]
        name_strs = []
        for orig_name, imported_name in sorted(names):
            if imported_name != orig_name:
                name_strs.append(f"{orig_name} as {imported_name}")
            else:
                name_strs.append(orig_name)
        new_lines.append(f"from {module} import {', '.join(name_strs)}\n")

    if not new_lines:
        return 0

    if dry_run:
        return len(to_move)

    lines = source.splitlines(keepends=True)

    # Find TC block start and insert before it
    insert_idx = tc_ranges[0][0] - 1  # Line before `if TYPE_CHECKING:`
    # Make sure there's a blank line
    if insert_idx > 0 and lines[insert_idx - 1].strip():
        new_lines.insert(0, "\n")

    for i, imp_line in enumerate(new_lines):
        lines.insert(insert_idx + i, imp_line)

    filepath.write_text(''.join(lines))
    return len(to_move)

def main():
    dry_run = "--dry-run" in sys.argv
    src_dir = Path("src/nexus")

    if not src_dir.exists():
        print(f"ERROR: {src_dir} not found.")
        sys.exit(1)

    total = 0
    py_files = sorted(src_dir.rglob("*.py"))
    print(f"Scanning {len(py_files)} Python files for TC imports used at runtime...")

    for filepath in py_files:
        count = fix_file(filepath, dry_run)
        if count:
            total += count
            prefix = "[DRY-RUN] " if dry_run else ""
            print(f"  {prefix}Moved {count} import(s) to runtime in {filepath.relative_to(src_dir.parent.parent)}")

    print(f"\nTotal imports moved: {total}")
    print(f"{'DRY RUN - no files modified' if dry_run else 'Files updated.'}")

if __name__ == "__main__":
    main()
