#!/usr/bin/env python3
"""Batch-update NexusFS constructor calls: replace db_path= with explicit store injection.

Transforms:
    NexusFS(
        backend=backend,
        db_path=some_value,
        ...
    )
Into:
    NexusFS(
        backend=backend,
        metadata_store=SQLAlchemyMetadataStore(db_path=some_value),
        record_store=SQLAlchemyRecordStore(db_path=some_value),
        ...
    )

And adds the necessary imports to each modified file.

Usage:
    python scripts/batch_update_db_path.py          # dry-run (default)
    python scripts/batch_update_db_path.py --apply  # actually modify files
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = [
    PROJECT_ROOT / "tests",
    PROJECT_ROOT / "scripts",
    PROJECT_ROOT / "benchmarks",
]

SKIP_FILES = {"conftest.py", "batch_update_db_path.py"}

IMPORT_META = "from nexus.storage.sqlalchemy_metadata_store import SQLAlchemyMetadataStore"
IMPORT_RECORD = "from nexus.storage.record_store import SQLAlchemyRecordStore"


def find_py_files() -> list[Path]:
    files = []
    for d in SCAN_DIRS:
        if d.exists():
            files.extend(sorted(d.rglob("*.py")))
    return [f for f in files if f.name not in SKIP_FILES]


def extract_nexusfs_arg_block(content: str, start: int) -> tuple[int, str]:
    """From position after 'NexusFS(', find matching ')' and return (end_pos, arg_text)."""
    depth = 1
    i = start
    while i < len(content) and depth > 0:
        if content[i] == "(":
            depth += 1
        elif content[i] == ")":
            depth -= 1
        i += 1
    return i, content[start : i - 1]  # exclude final ')'


def parse_value_expr(text: str, start: int) -> int:
    """Parse a Python expression starting at `start`, return end position."""
    depth = 0
    i = start
    in_str = None
    esc = False
    while i < len(text):
        ch = text[i]
        if esc:
            esc = False
            i += 1
            continue
        if ch == "\\":
            esc = True
            i += 1
            continue
        if in_str:
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ('"', "'"):
            in_str = ch
            i += 1
            continue
        if ch in ("(", "[", "{"):
            depth += 1
        elif ch in (")", "]", "}"):
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i += 1
    return i


def transform_content(content: str) -> tuple[str, list[str]]:
    """Transform all NexusFS(..db_path=..) calls. Returns (new_content, changes)."""
    changes = []
    result = []
    pos = 0

    for m in re.finditer(r"NexusFS\s*\(", content):
        call_start = m.start()
        arg_start = m.end()
        call_end, arg_block = extract_nexusfs_arg_block(content, arg_start)

        if "db_path=" not in arg_block:
            continue

        # Find db_path= in arg_block
        db_m = re.search(r"(\n?([ \t]*))db_path\s*=\s*", arg_block)
        if not db_m:
            continue

        leading = db_m.group(1)  # newline + indent
        indent = db_m.group(2)  # just the whitespace
        val_start = db_m.end()
        val_end = parse_value_expr(arg_block, val_start)
        value = arg_block[val_start:val_end].strip()

        has_comma = val_end < len(arg_block) and arg_block[val_end] == ","
        replace_end = val_end + 1 if has_comma else val_end

        new_text = (
            f"{leading}metadata_store=SQLAlchemyMetadataStore(db_path={value}),\n"
            f"{indent}record_store=SQLAlchemyRecordStore(db_path={value}){',' if has_comma else ''}"
        )

        new_arg_block = arg_block[: db_m.start()] + new_text + arg_block[replace_end:]
        new_call = content[call_start:arg_start] + new_arg_block + ")"

        result.append(content[pos:call_start])
        result.append(new_call)
        pos = call_end

        line_num = content[:call_start].count("\n") + 1
        changes.append(f"  L{line_num}: db_path={value}")

    result.append(content[pos:])
    return "".join(result), changes


def add_imports(content: str) -> tuple[str, list[str]]:
    """Add missing imports. Returns (new_content, import_changes)."""
    adds = []
    if IMPORT_META not in content:
        adds.append(IMPORT_META)
    if IMPORT_RECORD not in content:
        adds.append(IMPORT_RECORD)
    if not adds:
        return content, []

    # Find last 'from nexus.' import line
    lines = content.split("\n")
    insert_after = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("from nexus.") or s.startswith("import nexus"):
            insert_after = i

    if insert_after == -1:
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith("from ") or s.startswith("import "):
                insert_after = i

    if insert_after == -1:
        insert_after = 0

    for j, imp in enumerate(adds):
        lines.insert(insert_after + 1 + j, imp)

    return "\n".join(lines), [f"  + {a}" for a in adds]


def process_file(fp: Path, apply: bool) -> bool:
    content = fp.read_text(encoding="utf-8")
    if "NexusFS(" not in content or "db_path=" not in content:
        return False

    new_content, changes = transform_content(content)
    if not changes:
        return False

    new_content, imp_changes = add_imports(new_content)

    rel = fp.relative_to(PROJECT_ROOT)
    action = "MODIFYING" if apply else "WOULD MODIFY"
    print(f"\n{action}: {rel}")
    for ic in imp_changes:
        print(f"  Import: {ic}")
    for c in changes:
        print(f"  Change: {c}")

    if apply:
        fp.write_text(new_content, encoding="utf-8")
        print("  -> Written.")

    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually modify files")
    args = parser.parse_args()

    if not args.apply:
        print("=" * 60)
        print("DRY RUN - use --apply to write changes.")
        print("=" * 60)

    count = 0
    for fp in find_py_files():
        try:
            if process_file(fp, args.apply):
                count += 1
        except Exception as e:
            print(f"\nERROR: {fp}: {e}", file=sys.stderr)
            raise

    action = "Modified" if args.apply else "Would modify"
    print(f"\n{action} {count} file(s).")


if __name__ == "__main__":
    main()
