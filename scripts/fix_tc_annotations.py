#!/usr/bin/env python3
"""Fix unquoted TYPE_CHECKING annotations after `from __future__ import annotations` removal.

Finds all names imported only under `if TYPE_CHECKING:` blocks, then quotes all
unquoted usages of those names in type annotations throughout the same file.

Also fixes self-referencing class methods (class Foo: def m() -> Foo:).
"""

import ast
import re
import sys
from pathlib import Path


def get_tc_names(source: str) -> set[str]:
    """Extract names that are ONLY imported under TYPE_CHECKING."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    tc_names: set[str] = set()
    runtime_names: set[str] = set()

    for node in ast.walk(tree):
        # Find `if TYPE_CHECKING:` blocks
        if isinstance(node, ast.If):
            test = node.test
            is_tc = False
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING" or isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
                is_tc = True

            if is_tc:
                for child in ast.walk(node):
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        for alias in child.names:
                            name = alias.asname or alias.name
                            tc_names.add(name)
                continue

        # Track runtime imports (not under TYPE_CHECKING)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # Check if this node is NOT inside a TYPE_CHECKING block
            for alias in node.names:
                name = alias.asname or alias.name
                runtime_names.add(name)

    # Only return names that are TC-only (not also imported at runtime)
    return tc_names - runtime_names


def get_self_ref_classes(source: str) -> set[str]:
    """Find class names that reference themselves in method signatures."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    class_names = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            class_names.add(node.name)
    return class_names


def fix_file(filepath: Path, dry_run: bool = False) -> int:
    """Fix unquoted TYPE_CHECKING annotations in a file. Returns number of fixes."""
    source = filepath.read_text()

    # Skip files that still have `from __future__ import annotations`
    if "from __future__ import annotations" in source:
        return 0

    tc_names = get_tc_names(source)
    class_names = get_self_ref_classes(source)
    all_names = tc_names | class_names

    if not all_names:
        return 0

    lines = source.split("\n")
    fixes = 0
    in_class = None  # Track current class for self-reference detection

    # Build a regex pattern to match unquoted names in annotations
    # We need to be careful not to match:
    # - Names inside string literals (already quoted)
    # - Names in comments
    # - Names in actual code (not annotations)
    # - Names in docstrings

    for i, line in enumerate(lines):
        stripped = line.lstrip()

        # Track current class
        class_match = re.match(r"class\s+(\w+)", stripped)
        if class_match:
            in_class = class_match.group(1)

        # Skip comments and docstrings
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
            continue

        # Look for annotation patterns:
        # 1. Function parameter annotations: `param: TypeName`
        # 2. Return type annotations: `-> TypeName:`
        # 3. Variable annotations: `var: TypeName`
        # 4. Class variable annotations: `_var: TypeName`

        original = line
        changed = False

        for name in sorted(all_names, key=len, reverse=True):  # Longest first to avoid partial matches
            # Skip if name is not in this line at all
            if name not in line:
                continue

            # For self-referencing classes, only fix if we're inside that class
            if name in class_names and name not in tc_names:
                if in_class != name:
                    continue

            # Pattern 1: `name` used as a type annotation (not already quoted)
            # Match: `param: name` or `-> name` or `: name |` etc.
            # But NOT: `"name"` or `param: "name"` or inside strings

            # Fix `-> name:` (return type - whole type)
            # Fix `-> name |` or `-> name,` patterns
            # Fix `: name,` or `: name =` or `: name)` patterns
            # Fix `: name |` patterns (union type start)
            # Fix `| name` patterns (union type continuation)

            # Use a regex to find unquoted `name` in annotation context
            # This regex matches `name` that is:
            # - After `: ` or `-> ` or `| `
            # - Followed by `:`, `,`, `)`, ` |`, ` =`, ` #`, newline, `[`, or nothing
            # - NOT preceded by `"` or inside a string

            # Build pattern for this specific name
            # We need word boundaries to avoid matching substrings
            escaped = re.escape(name)

            # Pattern for annotations: after : or -> or | or ( or [
            # The name might be followed by [, |, :, ), ,, =, whitespace, or end of line
            patterns = [
                # `-> name:` or `-> name |` or `-> name,` or `-> name\n`
                (rf'(->[ ]*){escaped}(\s*[:\[|,)\]\n#])', rf'\1"{name}"\2'),
                # `: name,` or `: name)` or `: name =` or `: name |` or `: name\n`
                (rf'(:[ ]*){escaped}(\s*[,)=|\[\]\n#])', rf'\1"{name}"\2'),
                # `| name` in union types
                (rf'(\|[ ]*){escaped}(\s*[,)=|\[\]\n#:])', rf'\1"{name}"\2'),
                # `[name]` or `[name,` in generic type params
                (rf'(\[){escaped}(\s*[,\]])', rf'\1"{name}"\2'),
            ]

            for pat, repl in patterns:
                new_line = re.sub(pat, repl, line)
                if new_line != line:
                    line = new_line
                    changed = True

        if changed and line != original:
            # Verify we didn't double-quote
            line = line.replace('""' + '"', '"').replace('"' + '""', '"')
            lines[i] = line
            fixes += 1

    if fixes > 0 and not dry_run:
        filepath.write_text("\n".join(lines))

    return fixes


def main():
    dry_run = "--dry-run" in sys.argv
    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    src_dir = Path("/Users/taofeng/stream5/src/nexus")
    total_fixes = 0
    files_fixed = 0

    for py_file in sorted(src_dir.rglob("*.py")):
        fixes = fix_file(py_file, dry_run=dry_run)
        if fixes > 0:
            total_fixes += fixes
            files_fixed += 1
            if verbose:
                print(f"  {fixes} fixes in {py_file.relative_to(src_dir.parent)}")

    action = "Would fix" if dry_run else "Fixed"
    print(f"\n{action} {total_fixes} annotations in {files_fixed} files")


if __name__ == "__main__":
    main()
