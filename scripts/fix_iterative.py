"""Iteratively fix forward-ref NameErrors by trying to import and fixing each one.

Usage:
    python scripts/fix_iterative.py <import_statement> [max_iterations]

Example:
    python scripts/fix_iterative.py "from nexus.server.fastapi_server import create_app" 500
"""

import re
import subprocess
import sys
from pathlib import Path


def try_import(import_stmt: str) -> tuple[bool, str]:
    """Try to run an import and return (success, stderr)."""
    result = subprocess.run(
        ["/opt/homebrew/bin/python3.13", "-c", import_stmt],
        capture_output=True, text=True,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
        timeout=30,
    )
    if result.returncode == 0:
        return True, ""
    return False, result.stderr

def parse_name_error(stderr: str) -> tuple[str | None, str | None, int | None]:
    """Parse NameError from stderr. Returns (filepath, name, lineno)."""
    name_match = re.search(r"NameError: name '(\w+)' is not defined", stderr)
    if not name_match:
        return None, None, None
    error_name = name_match.group(1)
    file_matches = list(re.finditer(r'File "([^"]+)", line (\d+)', stderr))
    if not file_matches:
        return None, error_name, None
    last_file = file_matches[-1]
    return last_file.group(1), error_name, int(last_file.group(2))

def parse_type_error(stderr: str) -> tuple[str | None, int | None]:
    """Parse TypeError for 'str | NoneType' from stderr. Returns (filepath, lineno)."""
    if "unsupported operand type(s) for |:" not in stderr:
        return None, None
    file_matches = list(re.finditer(r'File "([^"]+)", line (\d+)', stderr))
    if not file_matches:
        return None, None
    last_file = file_matches[-1]
    return last_file.group(1), int(last_file.group(2))

def parse_module_not_found(stderr: str) -> tuple[str | None, str | None, int | None]:
    """Parse ModuleNotFoundError. Returns (filepath_of_importer, bad_module, lineno)."""
    mod_match = re.search(r"ModuleNotFoundError: No module named '([^']+)'", stderr)
    if not mod_match:
        return None, None, None
    bad_module = mod_match.group(1)
    file_matches = list(re.finditer(r'File "([^"]+)", line (\d+)', stderr))
    if not file_matches:
        return None, bad_module, None
    last_file = file_matches[-1]
    return last_file.group(1), bad_module, int(last_file.group(2))

def fix_name_error(filepath: str, name: str, lineno: int) -> bool:
    """Quote a bare name reference on the given line."""
    path = Path(filepath)
    if not path.exists():
        return False

    lines = path.read_text().splitlines(keepends=True)
    line_idx = lineno - 1
    if not (0 <= line_idx < len(lines)):
        return False

    line = lines[line_idx]
    original = line

    # Quote bare name in type annotation context
    # Pattern: bare name not already quoted, optionally followed by .Attr and/or | None
    pattern = re.compile(
        r'(?<!["\w\.])' + re.escape(name) + r'(?:\.\w+)*'  # name + optional .Attr chain
        r'(?:\s*\|\s*\w+(?:\.\w+)*)*'  # optional | None or | Type
        r'(?!["\w])'
    )
    matches = list(pattern.finditer(line))

    for match in reversed(matches):
        start, end = match.start(), match.end()
        full_match = match.group(0)
        # Skip if already quoted
        if start > 0 and line[start-1] == '"':
            continue
        # Skip if it's in a string literal (crude check)
        before = line[:start]
        if before.count('"') % 2 == 1:
            continue
        # Insert quotes around the full expression
        line = line[:start] + '"' + full_match + '"' + line[end:]

    if line != original:
        lines[line_idx] = line
        path.write_text(''.join(lines))
        return True

    return False

def fix_str_union(filepath: str, lineno: int) -> bool:
    """Fix '"ClassName" | None' -> '"ClassName | None"' on the given line."""
    path = Path(filepath)
    if not path.exists():
        return False

    lines = path.read_text().splitlines(keepends=True)
    line_idx = lineno - 1
    if not (0 <= line_idx < len(lines)):
        return False

    line = lines[line_idx]
    original = line

    # Fix "Name" | None -> "Name | None"
    line = re.sub(r'"([^"]+)"\s*\|\s*None', r'"\1 | None"', line)
    # Fix "Name" | str, "Name" | int, etc.
    line = re.sub(r'"([^"]+)"\s*\|\s*(\w+)', r'"\1 | \2"', line)

    if line != original:
        lines[line_idx] = line
        path.write_text(''.join(lines))
        return True

    return False

def fix_bad_runtime_import(filepath: str, lineno: int) -> bool:
    """Remove a runtime import that should stay under TYPE_CHECKING and quote usages."""
    path = Path(filepath)
    if not path.exists():
        return False

    lines = path.read_text().splitlines(keepends=True)
    line_idx = lineno - 1
    if not (0 <= line_idx < len(lines)):
        return False

    line = lines[line_idx]
    # Comment out or remove the bad import line
    if line.strip().startswith("from ") and "import " in line:
        lines[line_idx] = ""  # Remove the line
        path.write_text(''.join(lines))
        return True

    return False

def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/fix_iterative.py <import_statement> [max_iterations]")
        sys.exit(1)

    import_stmt = sys.argv[1]
    max_iters = int(sys.argv[2]) if len(sys.argv) > 2 else 500

    print(f"Testing: {import_stmt}")
    print(f"Max iterations: {max_iters}")
    print("=" * 60)

    fixed_count = 0
    seen_errors: set[tuple] = set()

    for i in range(max_iters):
        success, stderr = try_import(import_stmt)

        if success:
            print(f"\n{'='*60}")
            print(f"SUCCESS after {fixed_count} fixes!")
            return

        # Try NameError first
        filepath, name, lineno = parse_name_error(stderr)
        if filepath and name and lineno:
            error_key = ("name", filepath, name, lineno)
            if error_key in seen_errors:
                print(f"\nSTUCK on: {filepath}:{lineno} NameError: {name}")
                print(stderr[-500:])
                return
            seen_errors.add(error_key)
            if fix_name_error(filepath, name, lineno):
                fixed_count += 1
                print(f"  [{fixed_count}] Quoted '{name}' in {filepath}:{lineno}")
                continue
            else:
                print(f"\nCould not auto-fix: {filepath}:{lineno} NameError: {name}")
                print(stderr[-500:])
                return

        # Try TypeError (str | NoneType)
        filepath, lineno = parse_type_error(stderr)
        if filepath and lineno:
            error_key = ("type", filepath, lineno)
            if error_key in seen_errors:
                print(f"\nSTUCK on: {filepath}:{lineno} TypeError: str | union")
                print(stderr[-500:])
                return
            seen_errors.add(error_key)
            if fix_str_union(filepath, lineno):
                fixed_count += 1
                print(f"  [{fixed_count}] Fixed str|union in {filepath}:{lineno}")
                continue
            else:
                print(f"\nCould not fix str|union: {filepath}:{lineno}")
                print(stderr[-500:])
                return

        # Try ModuleNotFoundError
        filepath, bad_module, lineno = parse_module_not_found(stderr)
        if filepath and lineno and bad_module:
            error_key = ("module", filepath, bad_module, lineno)
            if error_key in seen_errors:
                print(f"\nSTUCK on: {filepath}:{lineno} ModuleNotFoundError: {bad_module}")
                print(stderr[-500:])
                return
            seen_errors.add(error_key)
            if fix_bad_runtime_import(filepath, lineno):
                fixed_count += 1
                print(f"  [{fixed_count}] Removed bad import '{bad_module}' in {filepath}:{lineno}")
                continue
            else:
                print(f"\nCould not fix module error: {filepath}:{lineno} {bad_module}")
                print(stderr[-500:])
                return

        # Unknown error
        print("\nUnhandled error type:")
        print(stderr[-800:])
        return

    print(f"\nReached max iterations ({max_iters})")

if __name__ == "__main__":
    main()
