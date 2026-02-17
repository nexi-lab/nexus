#!/usr/bin/env python3
"""Iteratively fix forward-reference NameErrors by importing modules and quoting names.

Strategy:
1. Try to import every module under src/nexus/
2. Catch NameError at the exact source file+line
3. Quote the offending name in the annotation
4. Repeat until convergence
"""

import importlib
import os
import re
import sys
import traceback


def get_all_modules():
    """Get all .py modules under src/nexus/."""
    modules = []
    for root, _dirs, files in os.walk("src/nexus"):
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            mod = path.replace("src/", "").replace("/", ".").replace(".py", "")
            if mod.endswith(".__init__"):
                mod = mod[:-9]
            modules.append(mod)
    return modules


def try_import_all():
    """Try importing all modules, return list of unique error locations."""
    errors = {}
    for mod_name in get_all_modules():
        if mod_name in sys.modules:
            continue
        try:
            importlib.import_module(mod_name)
        except NameError as e:
            tb = traceback.extract_tb(sys.exc_info()[2])
            if tb:
                source = tb[-1]
                key = (source.filename, source.lineno)
                if key not in errors:
                    name_str = str(e)
                    # Extract name from "name 'Foo' is not defined"
                    if "'" in name_str:
                        name = name_str.split("'")[1]
                    else:
                        name = name_str.split("name ")[1].split(" ")[0]
                    errors[key] = {"name": name, "file": source.filename, "line": source.lineno}
        except SyntaxError as e:
            if e.filename and e.lineno:
                key = (e.filename, e.lineno)
                if key not in errors:
                    errors[key] = {"name": "", "file": e.filename, "line": e.lineno, "type": "SyntaxError"}
        except TypeError as e:
            tb = traceback.extract_tb(sys.exc_info()[2])
            if tb:
                source = tb[-1]
                key = (source.filename, source.lineno)
                if key not in errors:
                    errors[key] = {"name": "", "file": source.filename, "line": source.lineno,
                                   "type": "TypeError", "msg": str(e)}
        except Exception:
            pass

    return list(errors.values())


def fix_name_at_line(filepath, lineno, name):
    """Quote a name in annotation context at the given line."""
    try:
        lines = open(filepath).read().split("\n")
    except Exception:
        return False

    idx = lineno - 1
    if idx < 0 or idx >= len(lines):
        return False

    line = lines[idx]

    # Already quoted?
    if f'"{name}"' in line or f'"{name} |' in line or f'| {name}"' in line:
        return False

    # Skip f-strings
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return False

    original = line

    # Try to match and fix union patterns first (must quote entire union)
    # Pattern: Name | Type1 | Type2 | None → "Name | Type1 | Type2 | None"
    # We need to find the full union expression containing `name`

    # Check for: `name | None` → `"name | None"`
    pattern_name_none = re.compile(
        rf'(?<!["\w]){re.escape(name)}\s*\|\s*None\b'
    )
    if pattern_name_none.search(line):
        line = pattern_name_none.sub(f'"{name} | None"', line)
        if line != original:
            lines[idx] = line
            open(filepath, "w").write("\n".join(lines))
            return True

    # Pattern: `name | SomeType | None`
    pattern_name_type_none = re.compile(
        rf'(?<!["\w]){re.escape(name)}\s*\|\s*([A-Z][A-Za-z0-9_]*)\s*\|\s*None\b'
    )
    if pattern_name_type_none.search(line):
        line = pattern_name_type_none.sub(rf'"{name} | \1 | None"', line)
        if line != original:
            lines[idx] = line
            open(filepath, "w").write("\n".join(lines))
            return True

    # Pattern: `name | SomeType` (no None)
    pattern_name_type = re.compile(
        rf'(?<!["\w]){re.escape(name)}\s*\|\s*([A-Z][A-Za-z0-9_]*)\b(?!\s*\|)'
    )
    if pattern_name_type.search(line):
        line = pattern_name_type.sub(rf'"{name} | \1"', line)
        if line != original:
            lines[idx] = line
            open(filepath, "w").write("\n".join(lines))
            return True

    # Pattern: `SomeType | name` where SomeType is the start of a union
    # This needs SomeType to be a real type. We quote the whole thing.
    pattern_type_name = re.compile(
        rf'([A-Z][A-Za-z0-9_]*)\s*\|\s*{re.escape(name)}\b'
    )
    if pattern_type_name.search(line):
        # Need to quote the whole union expression
        # Find the annotation start (after : or ->)
        m = re.search(r'(:\s*|->?\s*)', line)
        if m:
            ann_start = m.end()
            ann_text = line[ann_start:]
            # Find annotation end (before = or , or : at end)
            ann_end_match = re.search(r'\s*[=,]|\s*$|\s*:\s*$', ann_text)
            if ann_end_match:
                ann_end = ann_end_match.start()
                ann = ann_text[:ann_end].strip()
                if name in ann and f'"{name}"' not in ann and not ann.startswith('"'):
                    quoted_ann = f'"{ann}"'
                    line = line[:ann_start] + quoted_ann + ann_text[ann_end:]
                    if line != original:
                        lines[idx] = line
                        open(filepath, "w").write("\n".join(lines))
                        return True

    # Pattern: `Mapped[name | None]` or `list[name]` etc.
    pattern_generic = re.compile(
        rf'(\w+\[){re.escape(name)}(\s*[\]|,])'
    )
    m = pattern_generic.search(line)
    if m:
        # Check if it's name | None inside brackets
        pattern_generic_union = re.compile(
            rf'(\w+\[){re.escape(name)}\s*\|\s*None(\s*\])'
        )
        m2 = pattern_generic_union.search(line)
        if m2:
            line = pattern_generic_union.sub(rf'\1"{name} | None"\2', line)
        else:
            line = pattern_generic.sub(rf'\1"{name}"\2', line)
        if line != original:
            lines[idx] = line
            open(filepath, "w").write("\n".join(lines))
            return True

    # Simple: just quote the bare name
    pattern_bare = re.compile(rf'(?<!["\w.]){re.escape(name)}(?!["\w])')
    if pattern_bare.search(line):
        line = pattern_bare.sub(f'"{name}"', line, count=1)
        if line != original:
            lines[idx] = line
            open(filepath, "w").write("\n".join(lines))
            return True

    return False


def fix_fstring_at_line(filepath, lineno):
    """Fix f-string corruption at a specific line."""
    try:
        content = open(filepath).read()
    except Exception:
        return False

    # Generic pattern for f-string corruption: f"["Name"]..." → f"[Name]..."
    pattern = re.compile(r'f"(\[)"([A-Za-z_]\w*)"\]')
    new_content = pattern.sub(r'f"\1\2]', content)

    # Also: regular string "["Name"]..." → "[Name]..."
    pattern2 = re.compile(r'"(\[)"([A-Za-z_]\w*)"\]')
    new_content = pattern2.sub(r'"\1\2]', new_content)

    if new_content != content:
        open(filepath, "w").write(new_content)
        return True
    return False


def fix_str_subscript(filepath):
    """Fix "Name"[T] → "Name[T]" patterns."""
    try:
        content = open(filepath).read()
    except Exception:
        return False

    pattern = re.compile(r'"([A-Za-z_]\w*)"\[([A-Za-z0-9_.,\s\[\]|]+)\]')
    new_content = pattern.sub(r'"\1[\2]"', content)
    if new_content != content:
        open(filepath, "w").write(new_content)
        return True
    return False


def main():
    max_rounds = 50
    total_fixes = 0

    for round_num in range(1, max_rounds + 1):
        # Clear cached modules for re-import
        to_remove = [k for k in sys.modules if k.startswith("nexus.")]
        for k in to_remove:
            del sys.modules[k]

        errors = try_import_all()

        name_errors = [e for e in errors if e.get("type") != "SyntaxError" and e.get("type") != "TypeError"]
        syntax_errors = [e for e in errors if e.get("type") == "SyntaxError"]
        type_errors = [e for e in errors if e.get("type") == "TypeError"]

        total_errors = len(errors)
        if total_errors == 0:
            print(f"\nRound {round_num}: All importable modules succeed!")
            break

        print(f"\nRound {round_num}: {total_errors} unique error locations "
              f"({len(name_errors)} NameError, {len(syntax_errors)} SyntaxError, {len(type_errors)} TypeError)")

        fixes = 0

        for err in syntax_errors:
            if fix_fstring_at_line(err["file"], err["line"]):
                fixes += 1
                print(f"  Fixed SyntaxError at {err['file']}:{err['line']}")

        for err in type_errors:
            if fix_str_subscript(err["file"]):
                fixes += 1
                print(f"  Fixed TypeError at {err['file']}:{err['line']}")

        for err in name_errors:
            if fix_name_at_line(err["file"], err["line"], err["name"]):
                fixes += 1
                print(f"  Fixed {err['name']} at {err['file']}:{err['line']}")
            else:
                print(f"  SKIP {err['name']} at {err['file']}:{err['line']}")

        total_fixes += fixes
        print(f"  => {fixes} fixes this round (total: {total_fixes})")

        if fixes == 0:
            print(f"\nNo automatic fixes possible. {total_errors} errors remain.")
            for err in errors[:30]:
                name = err.get("name", err.get("msg", ""))
                etype = err.get("type", "NameError")
                print(f"  {etype}: {name} at {err['file']}:{err['line']}")
            break

    print(f"\nDone: {total_fixes} total fixes across {round_num} rounds")


if __name__ == "__main__":
    main()
