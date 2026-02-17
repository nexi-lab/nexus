"""Try importing every module under src/nexus/ and auto-fix NameErrors.

Runs fix_iterative.py logic on each discovered module.
"""
import importlib
import re
import subprocess
import sys
from pathlib import Path

def try_import(module_name: str) -> tuple[bool, str]:
    """Try to import a module and return (success, stderr)."""
    result = subprocess.run(
        ["/opt/homebrew/bin/python3.13", "-c", f"import {module_name}"],
        capture_output=True, text=True,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
        timeout=30,
    )
    if result.returncode == 0:
        return True, ""
    return False, result.stderr

def fix_name_error_on_line(filepath: str, name: str, lineno: int) -> bool:
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

    # Quote bare name + optional dotted attrs + optional | union
    pattern = re.compile(
        r'(?<!["\w\.])' + re.escape(name) + r'(?:\.\w+)*'
        r'(?:\s*\|\s*\w+(?:\.\w+)*)*'
        r'(?!["\w])'
    )
    matches = list(pattern.finditer(line))

    for match in reversed(matches):
        start, end = match.start(), match.end()
        full_match = match.group(0)
        if start > 0 and line[start-1] == '"':
            continue
        before = line[:start]
        if before.count('"') % 2 == 1:
            continue
        line = line[:start] + '"' + full_match + '"' + line[end:]

    if line != original:
        lines[line_idx] = line
        path.write_text(''.join(lines))
        return True
    return False

def fix_str_union_on_line(filepath: str, lineno: int) -> bool:
    """Fix '"ClassName" | None' patterns."""
    path = Path(filepath)
    if not path.exists():
        return False
    lines = path.read_text().splitlines(keepends=True)
    line_idx = lineno - 1
    if not (0 <= line_idx < len(lines)):
        return False
    line = lines[line_idx]
    original = line
    line = re.sub(r'"([^"]+)"\s*\|\s*None', r'"\1 | None"', line)
    line = re.sub(r'"([^"]+)"\s*\|\s*(\w+)', r'"\1 | \2"', line)
    if line != original:
        lines[line_idx] = line
        path.write_text(''.join(lines))
        return True
    return False

def fix_module(module_name: str, max_iters: int = 50) -> int:
    """Try to fix all NameErrors for a single module. Returns fix count."""
    fixes = 0
    seen = set()
    for _ in range(max_iters):
        ok, stderr = try_import(module_name)
        if ok:
            return fixes

        # NameError
        m = re.search(r"NameError: name '(\w+)' is not defined", stderr)
        if m:
            name = m.group(1)
            fm = list(re.finditer(r'File "([^"]+)", line (\d+)', stderr))
            if fm:
                fp, ln = fm[-1].group(1), int(fm[-1].group(2))
                key = (fp, name, ln)
                if key in seen:
                    return fixes  # stuck
                seen.add(key)
                if fix_name_error_on_line(fp, name, ln):
                    fixes += 1
                    continue
            return fixes

        # TypeError (str | NoneType)
        if "unsupported operand type(s) for |:" in stderr:
            fm = list(re.finditer(r'File "([^"]+)", line (\d+)', stderr))
            if fm:
                fp, ln = fm[-1].group(1), int(fm[-1].group(2))
                key = ("type", fp, ln)
                if key in seen:
                    return fixes
                seen.add(key)
                if fix_str_union_on_line(fp, ln):
                    fixes += 1
                    continue
            return fixes

        # Other errors - skip this module
        return fixes

    return fixes

def discover_modules(src_dir: Path) -> list[str]:
    """Discover all Python modules under src/nexus/."""
    modules = []
    for f in sorted(src_dir.rglob("*.py")):
        if f.name == "__init__.py":
            module = str(f.parent.relative_to(src_dir)).replace("/", ".")
        else:
            module = str(f.with_suffix("").relative_to(src_dir)).replace("/", ".")
        if module.startswith("nexus"):
            modules.append(module)
    return modules

def main():
    src_dir = Path("src")
    modules = discover_modules(src_dir)
    print(f"Found {len(modules)} modules")

    total_fixes = 0
    failed = []

    for i, mod in enumerate(modules):
        fixes = fix_module(mod)
        if fixes > 0:
            total_fixes += fixes
            print(f"  [{total_fixes}] Fixed {fixes} in {mod}")

        # Check if it still fails
        ok, stderr = try_import(mod)
        if not ok and "NameError" in stderr:
            failed.append(mod)

    print(f"\nTotal fixes: {total_fixes}")
    if failed:
        print(f"Still failing ({len(failed)}):")
        for m in failed[:20]:
            print(f"  - {m}")

if __name__ == "__main__":
    main()
