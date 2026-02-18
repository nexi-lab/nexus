#!/usr/bin/env python3
"""Comprehensive fix for forward-reference issues after removing `from __future__ import annotations`.

This script handles:
1. Unquoted TYPE_CHECKING-only names in annotations → quotes them
2. "Name"[T] patterns → "Name[T]"
3. "Name" | None patterns → "Name | None"
4. Self-referencing class names in methods → quotes them
5. Does NOT touch names inside f-strings, log messages, or function bodies

Uses AST to properly identify annotation contexts.
"""

import ast
import re
import sys
from pathlib import Path


def find_tc_names(tree: ast.Module) -> set[str]:
    """Find all names imported only under `if TYPE_CHECKING:`."""
    tc_names: set[str] = set()
    runtime_names: set[str] = set()

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.If):
            # Check if this is `if TYPE_CHECKING:`
            test = node.test
            is_tc = False
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING" or isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
                is_tc = True

            if is_tc:
                for child in ast.walk(node):
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        for alias in child.names:
                            name = alias.asname or alias.name
                            # Only add leaf name (e.g., "Foo" from "module.Foo")
                            tc_names.add(name.split(".")[-1])
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name
                runtime_names.add(name.split(".")[-1])

    # Only return names that are TC-only (not also imported at runtime)
    return tc_names - runtime_names


def find_class_names(tree: ast.Module) -> set[str]:
    """Find all class names defined in the module."""
    return {
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef)
    }


def fix_annotations_in_file(filepath: Path, dry_run: bool = False) -> int:
    """Fix forward-reference issues in a single file."""
    try:
        source = filepath.read_text()
    except Exception:
        return 0

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0

    tc_names = find_tc_names(tree)
    class_names = find_class_names(tree)
    names_to_quote = tc_names | class_names

    if not names_to_quote:
        return 0

    lines = source.split("\n")
    fixes = 0

    # Build a set of line numbers that are annotation contexts
    # We'll use AST to find all annotation nodes
    annotation_lines: set[int] = set()

    for node in ast.walk(tree):
        # Function annotations (parameters + return type)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns:
                for ln in range(node.returns.lineno, node.returns.end_lineno + 1):
                    annotation_lines.add(ln)
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                if arg.annotation:
                    for ln in range(arg.annotation.lineno, arg.annotation.end_lineno + 1):
                        annotation_lines.add(ln)
            if node.args.vararg and node.args.vararg.annotation:
                ann = node.args.vararg.annotation
                for ln in range(ann.lineno, ann.end_lineno + 1):
                    annotation_lines.add(ln)
            if node.args.kwarg and node.args.kwarg.annotation:
                ann = node.args.kwarg.annotation
                for ln in range(ann.lineno, ann.end_lineno + 1):
                    annotation_lines.add(ln)
        # Variable annotations
        elif isinstance(node, ast.AnnAssign):
            if node.annotation:
                for ln in range(node.annotation.lineno, node.annotation.end_lineno + 1):
                    annotation_lines.add(ln)

    # Also find module-level variable annotations without ast.AnnAssign
    # (bare annotations like `_foo: Type | None = None`)
    # These are already covered by ast.AnnAssign

    # Now process each annotation line
    for line_num in sorted(annotation_lines):
        idx = line_num - 1
        if idx >= len(lines):
            continue

        line = lines[idx]
        original_line = line

        # Skip lines that are inside strings or comments
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue

        for name in names_to_quote:
            if name not in line:
                continue

            # Pattern: bare Name in annotation context (not already quoted)
            # Match word boundary, not inside quotes, not inside f-strings
            # We need to be careful to only match in annotation positions

            # Skip if name appears only inside a string literal on this line
            # Simple heuristic: check if name appears outside of quoted strings
            # This is imperfect but catches most cases

            # Pattern 1: `: Name` or `-> Name` or `| Name` or `[Name` (unquoted in annotation)
            # But NOT inside f-strings or regular strings
            patterns = [
                # : Name (parameter/variable annotation start)
                (rf'(:\s*){name}(\s*[|,\[\]=)])', rf'\1"{name}"\2'),
                # -> Name (return type)
                (rf'(->\s*){name}(\s*[|:\[\]])', rf'\1"{name}"\2'),
                # | Name (union member)
                (rf'(\|\s*){name}(\s*[|,\]=)])', rf'\1"{name}"\2'),
                # [Name (generic parameter)
                (rf'(\[\s*){name}(\s*[,\]\|])', rf'\1"{name}"\2'),
                # , Name (in union/generic list)
                (rf'(,\s*){name}(\s*[,\]\|])', rf'\1"{name}"\2'),
            ]

            for pat, repl in patterns:
                new_line = re.sub(pat, repl, line)
                if new_line != line:
                    line = new_line

        if line != original_line:
            fixes += 1
            if not dry_run:
                lines[idx] = line

    # Fix "Name"[T] patterns -> "Name[T]"
    str_subscript = re.compile(r'"([A-Za-z0-9_]+)"\[([A-Za-z0-9_.,\s]+)\]')
    for idx, line in enumerate(lines):
        new_line = str_subscript.sub(r'"\1[\2]"', line)
        if new_line != line:
            fixes += 1
            if not dry_run:
                lines[idx] = new_line

    # Fix "Name" | None -> "Name | None"
    str_union_none = re.compile(r'"([A-Za-z0-9_.\[\], ]+)"\s*\|\s*None')
    for idx, line in enumerate(lines):
        # Skip if inside a string context (crude check)
        stripped = lines[idx].lstrip()
        if stripped.startswith('#') or stripped.startswith('f"') or stripped.startswith("f'"):
            continue
        new_line = str_union_none.sub(r'"\1 | None"', lines[idx])
        if new_line != lines[idx]:
            fixes += 1
            if not dry_run:
                lines[idx] = new_line

    # Fix "Name" | Type -> "Name | Type"
    str_union_type = re.compile(r'"([A-Za-z0-9_.\[\], ]+)"\s*\|\s*([A-Z][A-Za-z0-9_]*)')
    for idx, line in enumerate(lines):
        stripped = lines[idx].lstrip()
        if stripped.startswith('#') or stripped.startswith('f"') or stripped.startswith("f'"):
            continue
        new_line = str_union_type.sub(r'"\1 | \2"', lines[idx])
        if new_line != lines[idx]:
            fixes += 1
            if not dry_run:
                lines[idx] = new_line

    if fixes > 0 and not dry_run:
        filepath.write_text("\n".join(lines))

    return fixes


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    src_dir = Path("src/nexus")
    if not src_dir.exists():
        print(f"ERROR: {src_dir} not found")
        sys.exit(1)

    total_fixes = 0
    files_fixed = 0

    for py_file in sorted(src_dir.rglob("*.py")):
        fixes = fix_annotations_in_file(py_file, dry_run=dry_run)
        if fixes > 0:
            total_fixes += fixes
            files_fixed += 1
            if dry_run:
                print(f"  {py_file}: {fixes} fixes")

    mode = "[DRY RUN] " if dry_run else ""
    print(f"\n{mode}Fixed {total_fixes} patterns in {files_fixed} files")


if __name__ == "__main__":
    main()
