"""Fix '"ClassName" | None' and '"ClassName" | X' patterns across the codebase.

These occur when a forward-ref was quoted but the union operator still tries
to evaluate str | NoneType at runtime, which fails.
"""
import re
from pathlib import Path

# Pattern: "SomeName" | None  ->  "SomeName | None"
# Also: "SomeName" | str, "SomeName" | int, etc.
pattern = re.compile(r'"([^"]+)"\s*\|\s*(\w+)')

src_dir = Path("src/nexus")
total_fixes = 0

for f in sorted(src_dir.rglob("*.py")):
    content = f.read_text()
    new_lines = []
    changed = False
    for line in content.split("\n"):
        matches = list(pattern.finditer(line))
        if matches:
            for m in matches:
                old = m.group(0)
                inner = m.group(1)
                rhs = m.group(2)
                new = f'"{inner} | {rhs}"'
                line = line.replace(old, new)
                total_fixes += 1
                print(f"  {f.relative_to(src_dir.parent.parent)}: {old} -> {new}")
                changed = True
        new_lines.append(line)
    if changed:
        f.write_text("\n".join(new_lines))

print(f"\nTotal: {total_fixes} fixes")
