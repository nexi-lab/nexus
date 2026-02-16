#!/usr/bin/env python3
"""
Pre-commit hook and CI check to enforce zero-core-imports in bricks/.

LEGO Architecture Principle 3: "Bricks don't know about each other"
and bricks must never import from nexus.core (the kernel).

Bricks communicate with the kernel exclusively through protocols defined
in core/protocols/ and services/protocols/. Direct imports from nexus.core
or nexus.services internals are architectural violations.

Reference: docs/design/NEXUS-LEGO-ARCHITECTURE.md §1.2, Principle 3
"""

import re
import sys
from pathlib import Path

# Path to bricks directory relative to project root
BRICKS_RELATIVE_PATH = Path("src") / "nexus" / "bricks"

# Forbidden import patterns for files under bricks/
# Bricks may only import from:
#   - nexus.core.protocols.*  (kernel protocol interfaces)
#   - nexus.services.protocols.*  (system service protocol interfaces)
#   - nexus.storage.*  (storage pillar ABCs)
#   - Third-party packages
#   - Other bricks' public APIs (through protocols, not direct imports)
#
# Note: TYPE_CHECKING imports are also flagged intentionally — bricks should
# not even type-reference kernel internals (use protocols for type annotations).
# Multiline strings containing import-like text may produce false positives;
# this is a known limitation (unlikely in practice for brick modules).
FORBIDDEN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Direct core imports (excluding protocols — \b ensures protocols_extra etc. are caught)
    (re.compile(r"^\s*from\s+nexus\.core(?!\.protocols\b)"), "nexus.core"),
    (re.compile(r"^\s*import\s+nexus\.core(?!\.protocols\b)"), "nexus.core"),
    # Direct services imports (excluding protocols)
    (re.compile(r"^\s*from\s+nexus\.services(?!\.protocols\b)"), "nexus.services"),
    (re.compile(r"^\s*import\s+nexus\.services(?!\.protocols\b)"), "nexus.services"),
]

# Lines matching these patterns are not actual imports (comments, strings, etc.)
SKIP_PATTERNS = [
    re.compile(r"^\s*#"),  # Comments
    re.compile(r'^\s*["\']'),  # String literals
    re.compile(r"^\s*$"),  # Empty lines
]


def is_import_line(line: str) -> bool:
    """Check if a line is an actual import statement (not a comment or string)."""
    return not any(p.match(line) for p in SKIP_PATTERNS)


def check_file(file_path: Path) -> list[tuple[int, str, str]]:
    """
    Check a single file for forbidden imports.

    Returns:
        List of (line_number, line_content, matched_pattern_description) tuples.
    """
    violations = []
    try:
        with open(file_path, encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                if not is_import_line(line):
                    continue
                for pattern, desc in FORBIDDEN_PATTERNS:
                    if pattern.search(line):
                        violations.append((line_num, line.rstrip(), desc))
                        break  # One violation per line is enough
    except Exception as e:
        print(f"Warning: Could not read {file_path}: {e}")

    return violations


def find_brick_files(root: Path) -> list[Path]:
    """Find all Python files under the bricks/ directory."""
    bricks_dir = root / BRICKS_RELATIVE_PATH
    if not bricks_dir.exists():
        return []
    return sorted(bricks_dir.rglob("*.py"))


def main() -> int:
    """Main entry point for pre-commit hook and CI check.

    Usage:
        # CI mode: scan all bricks/ files automatically
        python check_brick_imports.py

        # Pre-commit mode: check specific files
        python check_brick_imports.py <file1> [file2] ...
    """
    if len(sys.argv) > 1:
        # Pre-commit mode: check specified files, filter to bricks/ only
        files = [
            Path(f)
            for f in sys.argv[1:]
            if f.endswith(".py") and "/bricks/" in f.replace("\\", "/")
        ]
    else:
        # CI mode: scan entire bricks/ directory
        files = find_brick_files(Path.cwd())

    if not files:
        # No brick files to check — this is expected until bricks/ is created
        return 0

    all_violations: list[tuple[Path, list[tuple[int, str, str]]]] = []

    for file_path in files:
        violations = check_file(file_path)
        if violations:
            all_violations.append((file_path, violations))

    if all_violations:
        print("\n❌ Brick import check failed!")
        print("\n🧱 Bricks must not import from kernel internals:\n")

        for file_path, violations in all_violations:
            print(f"  {file_path}:")
            for line_num, line_content, desc in violations:
                print(f"    Line {line_num}: {line_content}")
                print(f"             ↳ Forbidden: direct import from {desc}")
            print()

        print("📋 LEGO Architecture Principle 3: Bricks don't know about the kernel")
        print("🎯 Bricks may only import from:")
        print("     nexus.core.protocols.*      (kernel protocol interfaces)")
        print("     nexus.services.protocols.*   (system service protocol interfaces)")
        print("     nexus.storage.*              (storage pillar ABCs)")
        print()
        print("💡 See docs/design/NEXUS-LEGO-ARCHITECTURE.md §1.2")
        print()

        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
