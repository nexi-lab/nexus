"""Fix misplaced imports from batch_update_db_path.py.

The batch script inserted imports at wrong positions:
1. Inside function bodies (after lazy imports like `import traceback`)
2. Inside multi-line parenthesized imports (between `(` and `)`)

This script relocates them to the correct top-level import position.
"""

import sys
from pathlib import Path

IMPORT_PATTERNS = [
    "from nexus.storage.sqlalchemy_metadata_store import SQLAlchemyMetadataStore",
    "from nexus.storage.record_store import SQLAlchemyRecordStore",
    "from nexus.storage.raft_metadata_store import RaftMetadataStore",
]


def find_import_section_end(lines: list[str]) -> int:
    """Find the index of the last top-level import line (including closing parens).

    For multi-line imports like `from x import (...)`, returns the line with `)`.
    Returns -1 if no imports found.
    """
    last_import_end = -1
    in_docstring = False
    in_parens = 0

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Track docstrings
        if '"""' in stripped or "'''" in stripped:
            triple_count = stripped.count('"""') + stripped.count("'''")
            if triple_count == 1:
                in_docstring = not in_docstring
            continue

        if in_docstring:
            continue
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue

        # Inside parenthesized import
        if in_parens > 0:
            in_parens += stripped.count("(") - stripped.count(")")
            if in_parens <= 0:
                # Closing paren found - this is the end of the multi-line import
                last_import_end = i
                in_parens = 0
            continue

        at_col0 = not line[0:1].isspace()

        if at_col0:
            if stripped.startswith("from ") or stripped.startswith("import "):
                in_parens = stripped.count("(") - stripped.count(")")
                if in_parens <= 0:
                    last_import_end = i
                    in_parens = 0
                # else: multi-line import started, will be closed later
                continue

            # Allow module-level setup lines in the import section
            if stripped.startswith("if TYPE_CHECKING"):
                continue
            if stripped.startswith("try:") or stripped.startswith("except"):
                continue
            if "sys.path" in stripped:
                continue
            if "=" in stripped and any(
                stripped.startswith(v)
                for v in ["script_dir", "src_dir", "_src_path", "pytestmark", "collect_ignore"]
            ):
                continue

            # Anything else at column 0 ends the section
            break
        else:
            # Indented lines in TYPE_CHECKING / try blocks
            if stripped.startswith("from ") or stripped.startswith("import "):
                continue
            if stripped.startswith("collect_ignore"):
                continue

    return last_import_end


def is_inside_parens(lines: list[str], idx: int) -> bool:
    """Check if line at `idx` is inside a parenthesized import."""
    paren_depth = 0
    for i in range(idx):
        stripped = lines[i].strip()
        if stripped.startswith("#"):
            continue
        paren_depth += stripped.count("(") - stripped.count(")")
    return paren_depth > 0


def fix_file(filepath: Path, dry_run: bool = True) -> bool:
    """Fix misplaced imports in a single file. Returns True if modified."""
    content = filepath.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)

    import_section_end = find_import_section_end(lines)

    misplaced_indices = []
    imports_to_relocate = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped not in IMPORT_PATTERNS:
            continue

        # Type 1: After the import section (inside function bodies)
        if i > import_section_end:
            misplaced_indices.append(i)
            if stripped not in imports_to_relocate:
                imports_to_relocate.append(stripped)
            continue

        # Type 2: Inside a parenthesized import
        if is_inside_parens(lines, i):
            misplaced_indices.append(i)
            if stripped not in imports_to_relocate:
                imports_to_relocate.append(stripped)
            continue

    if not misplaced_indices:
        return False

    # Remove misplaced lines
    new_lines = [line for i, line in enumerate(lines) if i not in misplaced_indices]

    # Clean up triple+ blank lines
    cleaned = []
    blank_count = 0
    for line in new_lines:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 2:
                cleaned.append(line)
        else:
            blank_count = 0
            cleaned.append(line)

    # Find correct insertion point (after the import section ends)
    insert_idx = find_import_section_end(cleaned)
    if insert_idx < 0:
        insert_idx = 0

    # Avoid duplicates
    existing = set()
    for line in cleaned:
        stripped = line.strip()
        if not line[0:1].isspace() and stripped in IMPORT_PATTERNS:
            existing.add(stripped)

    imports_to_add = []
    for imp in imports_to_relocate:
        if imp not in existing:
            imports_to_add.append(imp + "\n")
            existing.add(imp)

    if imports_to_add:
        result = cleaned[: insert_idx + 1] + imports_to_add + cleaned[insert_idx + 1 :]
    else:
        result = cleaned

    result_content = "".join(result)
    if result_content == content:
        return False

    if dry_run:
        print(f"  WOULD FIX: {filepath.name}")
        print(
            f"    Remove {len(misplaced_indices)} misplaced import(s) from lines {[i + 1 for i in misplaced_indices]}"
        )
        if imports_to_add:
            print(f"    Add {len(imports_to_add)} import(s) after line {insert_idx + 1}")
    else:
        filepath.write_text(result_content, encoding="utf-8")
        print(f"  FIXED: {filepath}")

    return True


def main():
    dry_run = "--apply" not in sys.argv
    if dry_run:
        print("DRY RUN mode (use --apply to write changes)\n")

    project_root = Path(__file__).parent.parent
    patterns = ["tests/**/*.py", "scripts/**/*.py"]
    fixed = 0
    for pattern in patterns:
        for fp in sorted(project_root.glob(pattern)):
            if fp.name in ("fix_misplaced_imports.py", "batch_update_db_path.py"):
                continue
            try:
                if fix_file(fp, dry_run=dry_run):
                    fixed += 1
            except Exception as e:
                print(f"  ERROR: {fp.name}: {e}")

    print(f"\n{'Would fix' if dry_run else 'Fixed'} {fixed} files")


if __name__ == "__main__":
    main()
