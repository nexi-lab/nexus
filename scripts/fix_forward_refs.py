"""Fix forward-reference NameErrors after removing `from __future__ import annotations`.

Two patterns:
1. Self-referencing class methods: `class Foo: def m() -> Foo:` → `-> "Foo":`
2. TYPE_CHECKING imports used at runtime → move to regular imports

Usage:
    python scripts/fix_forward_refs.py [--dry-run]
"""

import ast
import re
import sys
from pathlib import Path


def find_self_ref_classes(source: str, filepath: str) -> list[tuple[int, str, str]]:
    """Find class methods that reference their own class name in annotations.

    Returns list of (line_number, old_text, new_text) replacements.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    replacements = []
    lines = source.splitlines()

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        class_name = node.name

        # Walk all function defs inside this class
        for child in ast.walk(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            # Check return annotation
            if child.returns:
                _check_annotation(child.returns, class_name, lines, replacements)

            # Check argument annotations
            for arg in child.args.args + child.args.posonlyargs + child.args.kwonlyargs:
                if arg.annotation:
                    _check_annotation(arg.annotation, class_name, lines, replacements)
            if child.args.vararg and child.args.vararg.annotation:
                _check_annotation(child.args.vararg.annotation, class_name, lines, replacements)
            if child.args.kwarg and child.args.kwarg.annotation:
                _check_annotation(child.args.kwarg.annotation, class_name, lines, replacements)


    return replacements


def _check_annotation(ann_node, class_name: str, lines: list[str], replacements: list):
    """Check if an annotation node references class_name and needs quoting."""
    if isinstance(ann_node, ast.Name) and ann_node.id == class_name:
        line_idx = ann_node.lineno - 1
        line = lines[line_idx]
        # Already quoted?
        col = ann_node.col_offset
        if col > 0 and line[col-1] == '"':
            return
        replacements.append((ann_node.lineno, ann_node.col_offset, class_name))

    elif isinstance(ann_node, ast.Subscript):
        # e.g. HandlerResponse[T] — check the value
        _check_annotation(ann_node.value, class_name, lines, replacements)

    elif isinstance(ann_node, ast.BinOp) and isinstance(ann_node.op, ast.BitOr):
        # Union: X | Y
        _check_annotation(ann_node.left, class_name, lines, replacements)
        _check_annotation(ann_node.right, class_name, lines, replacements)


def find_type_checking_runtime_usage(source: str) -> list[str]:
    """Find names imported under TYPE_CHECKING that are used at runtime.

    Returns list of module import lines that need to be moved to runtime.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    # Find TYPE_CHECKING block
    tc_names: dict[str, tuple[str, str]] = {}  # name -> (module, original_name)
    tc_block_lines: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            # Check if this is `if TYPE_CHECKING:`
            test = node.test
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                for stmt in node.body:
                    if isinstance(stmt, ast.ImportFrom) and stmt.module:
                        for alias in stmt.names:
                            imported_name = alias.asname or alias.name
                            tc_names[imported_name] = (stmt.module, alias.name)
                            tc_block_lines.add(stmt.lineno)
                    elif isinstance(stmt, ast.Import):
                        for alias in stmt.names:
                            imported_name = alias.asname or alias.name
                            tc_names[imported_name] = (alias.name, alias.name)
                            tc_block_lines.add(stmt.lineno)

    if not tc_names:
        return []

    # Now find which of these names are used at runtime (outside strings)
    # We look for Name nodes that reference tc_names outside of:
    # - TYPE_CHECKING blocks
    # - String annotations (already handled)
    runtime_used = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in tc_names:
            # Check if this is inside the TYPE_CHECKING block
            if node.lineno not in tc_block_lines:
                runtime_used.add(node.id)
        elif isinstance(node, ast.Attribute):
            # Check base of attribute access
            if isinstance(node.value, ast.Name) and node.value.id in tc_names:
                if node.value.lineno not in tc_block_lines:
                    runtime_used.add(node.value.id)

    return list(runtime_used)


def fix_file_self_refs(filepath: Path, dry_run: bool = False) -> int:
    """Fix self-referencing class annotations in a file.

    Returns number of fixes applied.
    """
    source = filepath.read_text()
    replacements = find_self_ref_classes(source, str(filepath))

    if not replacements:
        return 0

    lines = source.splitlines(keepends=True)

    # Sort replacements by line number descending, then col descending
    # so we can apply them without shifting offsets
    replacements.sort(key=lambda x: (x[0], x[1]), reverse=True)

    # Deduplicate by (line, col)
    seen = set()
    unique_replacements = []
    for lineno, col, name in replacements:
        key = (lineno, col)
        if key not in seen:
            seen.add(key)
            unique_replacements.append((lineno, col, name))

    count = 0
    for lineno, col, name in unique_replacements:
        line_idx = lineno - 1
        line = lines[line_idx]

        # Check if already quoted
        if col > 0 and col < len(line) and line[col-1] == '"':
            continue

        # Replace the bare name with quoted name
        # We need to handle cases like `-> ClassName:` and `-> ClassName[T]:`
        # Find the extent of the name in the line
        end_col = col + len(name)
        before = line[:col]
        after = line[end_col:]

        # Check if it's part of a subscript like ClassName[T]
        if after.lstrip().startswith('['):
            # Need to find the matching ] and quote the whole thing
            # This is complex, so for subscripts we quote just the name
            # Actually for self-refs like `-> HandlerResponse[T]:` inside the class,
            # we need to quote the whole expression. But if T is a TypeVar, the
            # subscript form works with a quoted class name.
            lines[line_idx] = before + '"' + name + '"' + after
        else:
            lines[line_idx] = before + '"' + name + '"' + after

        count += 1

    if count > 0 and not dry_run:
        filepath.write_text(''.join(lines))

    return count


def fix_file_tc_imports(filepath: Path, dry_run: bool = False) -> int:
    """Move TYPE_CHECKING imports to runtime if they're used at runtime.

    Returns number of names moved.
    """
    source = filepath.read_text()
    runtime_used = find_type_checking_runtime_usage(source)

    if not runtime_used:
        return 0

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0

    lines = source.splitlines(keepends=True)

    # Find the TYPE_CHECKING if block
    tc_if_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                tc_if_node = node
                break

    if tc_if_node is None:
        return 0

    # Collect import statements in the TC block
    # Group by module
    tc_imports: dict[str, list[tuple[str, str | None]]] = {}  # module -> [(name, asname)]
    tc_import_lines: list[int] = []

    for stmt in tc_if_node.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module:
            for alias in stmt.names:
                imported_name = alias.asname or alias.name
                if imported_name in runtime_used:
                    if stmt.module not in tc_imports:
                        tc_imports[stmt.module] = []
                    tc_imports[stmt.module].append((alias.name, alias.asname))

    if not tc_imports:
        return 0

    # Build new import lines to add before the TC block
    new_import_lines = []
    for module, names in sorted(tc_imports.items()):
        name_strs = []
        for name, asname in names:
            if asname and asname != name:
                name_strs.append(f"{name} as {asname}")
            else:
                name_strs.append(name)
        new_import_lines.append(f"from {module} import {', '.join(name_strs)}\n")

    if not new_import_lines and not dry_run:
        return 0

    # Find the line to insert before (the `if TYPE_CHECKING:` line)
    insert_line = tc_if_node.lineno - 1

    # Remove the moved names from TC block
    # This is complex to do correctly with AST, so we'll just add the runtime
    # imports and leave the TC imports (duplicates are harmless for TYPE_CHECKING)

    if not dry_run:
        for imp_line in reversed(new_import_lines):
            lines.insert(insert_line, imp_line)
        filepath.write_text(''.join(lines))

    return len(runtime_used)


def main():
    dry_run = "--dry-run" in sys.argv
    src_dir = Path("src/nexus")

    if not src_dir.exists():
        print(f"ERROR: {src_dir} not found. Run from project root.")
        sys.exit(1)

    total_self_ref = 0
    total_tc = 0

    py_files = sorted(src_dir.rglob("*.py"))
    print(f"Scanning {len(py_files)} Python files...")

    for filepath in py_files:
        count = fix_file_self_refs(filepath, dry_run)
        if count:
            total_self_ref += count
            prefix = "[DRY-RUN] " if dry_run else ""
            print(f"  {prefix}Fixed {count} self-ref(s) in {filepath.relative_to(src_dir.parent.parent)}")

    print(f"\nTotal self-ref fixes: {total_self_ref}")
    print(f"Total TYPE_CHECKING fixes: {total_tc}")
    print(f"{'DRY RUN - no files modified' if dry_run else 'Files updated.'}")


if __name__ == "__main__":
    main()
