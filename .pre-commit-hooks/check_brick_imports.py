#!/usr/bin/env python3
"""
Pre-commit hook and CI check to enforce brick import boundaries.

LEGO Architecture Principle 3: "Bricks don't know about each other"
and bricks must never import from nexus.core (the kernel).

Bricks communicate with the kernel exclusively through protocols defined
in core/protocols/ and contracts/protocols/. Direct imports from nexus.core
or nexus.services are architectural violations. Cross-brick
imports (bricks/<X>/ importing from nexus.bricks.<Y>) are also forbidden.

Reference: docs/design/NEXUS-LEGO-ARCHITECTURE.md §1.2, Principle 3
"""

import re
import sys
from pathlib import Path

# Path to bricks directory relative to project root
BRICKS_RELATIVE_PATH = Path("src") / "nexus" / "bricks"

# Forbidden import patterns for files under bricks/
# Bricks may only import from:
#   - nexus.core.protocols.*    (kernel protocol interfaces)
#   - nexus.contracts.cache_store (CacheStoreABC — kernel storage pillar)
#   - nexus.core.object_store   (ObjectStoreABC — kernel storage pillar)
#   - nexus.contracts.protocols.* (service protocol interfaces)
#   - nexus.storage.*           (storage pillar ABCs + RecordStoreABC)
#   - Third-party packages
#   - Same brick (nexus.bricks.<own_brick>.*)
#
# Note: TYPE_CHECKING imports are also flagged intentionally — bricks should
# not even type-reference kernel internals (use protocols for type annotations).
# Multiline strings containing import-like text may produce false positives;
# this is a known limitation (unlikely in practice for brick modules).
FORBIDDEN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Direct core imports (excluding protocols and storage-pillar ABCs)
    (
        re.compile(
            r"^\s*from\s+nexus\.core(?!\.protocols\b|\.cache_store\b|\.object_store\b|\.path_utils\b|\.nexus_fs\b)"
        ),
        "nexus.core",
    ),
    (
        re.compile(
            r"^\s*import\s+nexus\.core(?!\.protocols\b|\.cache_store\b|\.object_store\b|\.path_utils\b|\.nexus_fs\b)"
        ),
        "nexus.core",
    ),
    # Direct services imports (protocols moved to contracts/protocols/)
    (re.compile(r"^\s*from\s+nexus\.services"), "nexus.services"),
    (re.compile(r"^\s*import\s+nexus\.services"), "nexus.services"),
]

# Regex to extract the target brick name from a brick import line
CROSS_BRICK_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+nexus\.bricks\.(\w+)")

# Known cross-brick exceptions — temporary allowlist for imports that require
# DI refactoring to fix properly. Each entry maps (source_brick, target_brick)
# to a list of importing modules. Remove entries as fixes land.
# TODO(#2286): Fix memory->search via DI refactoring.
# TODO(#2364): Fix search->cache via DI refactoring for EmbeddingCache/Dragonfly.
KNOWN_CROSS_BRICK_EXCEPTIONS: dict[tuple[str, str], list[str]] = {
    ("memory", "search"): [
        "nexus.bricks.memory.enrichment",
        "nexus.bricks.memory.memory_with_paging",
        "nexus.bricks.memory.service",
    ],
    ("search", "cache"): [
        "nexus.bricks.search.embeddings",
    ],
    # search -> rebac: TYPE_CHECKING + lazy imports for permissions (moved from services/)
    ("search", "rebac"): [
        "nexus.bricks.search.search_service",
    ],
    # search -> memory: lazy imports for MemoryViewRouter (memory path listing)
    ("search", "memory"): [
        "nexus.bricks.search.search_service",
    ],
    # TODO(#2429): Fix a2a->ipc via DI refactoring.
    ("a2a", "ipc"): [
        "nexus.bricks.a2a.messaging_adapters",
    ],
    # TODO(#2429): Fix mcp->rebac/discovery via DI refactoring.
    ("mcp", "rebac"): [
        "nexus.bricks.mcp.middleware",
        "nexus.bricks.mcp.profiles",
    ],
    ("mcp", "discovery"): [
        "nexus.bricks.mcp.server",
    ],
    # TODO(#2429): Fix parsers->sandbox via DI refactoring.
    ("parsers", "sandbox"): [
        "nexus.bricks.parsers.validation.runner",
        "nexus.bricks.parsers.validation.detector",
    ],
    # TODO(#2429): Fix memory->llm via DI refactoring.
    ("memory", "llm"): [
        "nexus.bricks.memory.coref_resolver",
        "nexus.bricks.memory.relationship_extractor",
    ],
}

# Known exceptions for bricks importing from nexus.core (non-protocol) or
# nexus.services (non-protocol). Each entry maps a module name to a list of
# allowed forbidden-pattern descriptions. Remove entries as fixes land.
# TODO(#2429): Fix mcp.server -> nexus.core.filesystem via protocol/DI.
KNOWN_CORE_EXCEPTIONS: dict[str, list[str]] = {
    "nexus.bricks.mcp.server": ["nexus.core"],
    # search_service moved from services/ — needs core.metastore, core.router, core.path_utils,
    # and services.gateway (TYPE_CHECKING + lazy imports)
    "nexus.bricks.search.search_service": ["nexus.core", "nexus.services"],
    # dispatch_consumer needs core.pipe + core.pipe_manager for DT_PIPE lifecycle
    "nexus.bricks.task_manager.dispatch_consumer": ["nexus.core"],
}

# Lines matching these patterns are not actual imports (comments, strings, etc.)
SKIP_PATTERNS = [
    re.compile(r"^\s*#"),  # Comments
    re.compile(r'^\s*["\']'),  # String literals
    re.compile(r"^\s*$"),  # Empty lines
]


def is_import_line(line: str) -> bool:
    """Check if a line is an actual import statement (not a comment or string)."""
    return not any(p.match(line) for p in SKIP_PATTERNS)


def extract_brick_name(file_path: Path) -> str | None:
    """Extract brick name from file path.

    Example: .../bricks/memory/service.py -> 'memory'
    """
    parts = file_path.parts
    try:
        idx = parts.index("bricks")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    except ValueError:
        pass
    return None


def _module_name_from_path(file_path: Path) -> str | None:
    """Derive dotted module name from file path (best-effort).

    Example: .../src/nexus/bricks/memory/service.py -> 'nexus.bricks.memory.service'
    """
    parts = file_path.parts
    # Prefer the "nexus" that follows "src/" to avoid matching repo directory names
    # (e.g. on CI: /home/runner/work/nexus/nexus/src/nexus/bricks/...)
    try:
        src_idx = parts.index("src")
        mod_parts = list(parts[src_idx + 1 :])
    except ValueError:
        # No src/ in path; fall back to first "nexus"
        try:
            idx = parts.index("nexus")
        except ValueError:
            return None
        mod_parts = list(parts[idx:])
    # Strip .py from last part
    if mod_parts[-1].endswith(".py"):
        mod_parts[-1] = mod_parts[-1][:-3]
    # Strip __init__
    if mod_parts[-1] == "__init__":
        mod_parts = mod_parts[:-1]
    return ".".join(mod_parts)


def _is_cross_brick_exception(source_brick: str, target_brick: str, file_path: Path) -> bool:
    """Check if a cross-brick import is in the known exceptions allowlist."""
    allowed_modules = KNOWN_CROSS_BRICK_EXCEPTIONS.get((source_brick, target_brick))
    if allowed_modules is None:
        return False
    module_name = _module_name_from_path(file_path)
    return module_name in allowed_modules if module_name else False


def check_file(file_path: Path, brick_name: str | None = None) -> list[tuple[int, str, str]]:
    """Check a single file for forbidden imports.

    Args:
        file_path: Path to the Python file to check.
        brick_name: Name of the brick this file belongs to (for cross-brick checks).
            If None, cross-brick checks are skipped.

    Returns:
        List of (line_number, line_content, matched_pattern_description) tuples.
    """
    violations = []
    try:
        with open(file_path, encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                if not is_import_line(line):
                    continue

                # Check static forbidden patterns (core/services internals)
                found = False
                module_name = _module_name_from_path(file_path)
                for pattern, desc in FORBIDDEN_PATTERNS:
                    if pattern.search(line):
                        # Check if this module has a known core exception
                        allowed = KNOWN_CORE_EXCEPTIONS.get(module_name or "", [])
                        if desc not in allowed:
                            violations.append((line_num, line.rstrip(), desc))
                            found = True
                        break  # One violation per line is enough

                if found:
                    continue

                # Check cross-brick imports
                if brick_name:
                    m = CROSS_BRICK_IMPORT_RE.match(line)
                    if m:
                        target_brick = m.group(1)
                        if target_brick != brick_name and not _is_cross_brick_exception(
                            brick_name, target_brick, file_path
                        ):
                            violations.append(
                                (
                                    line_num,
                                    line.rstrip(),
                                    f"nexus.bricks.{target_brick} (cross-brick import)",
                                )
                            )
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
        # Pre-commit mode: check specified files, filter to src/bricks/ only
        # Test files (tests/) are excluded — test code legitimately imports
        # from core/services/other-bricks to set up fixtures and assertions.
        files = [
            Path(f)
            for f in sys.argv[1:]
            if f.endswith(".py")
            and "/bricks/" in f.replace("\\", "/")
            and not f.replace("\\", "/").startswith("tests/")
        ]
    else:
        # CI mode: scan entire bricks/ directory
        files = find_brick_files(Path.cwd())

    if not files:
        # No brick files to check — this is expected until bricks/ is created
        return 0

    all_violations: list[tuple[Path, list[tuple[int, str, str]]]] = []

    for file_path in files:
        brick_name = extract_brick_name(file_path)
        violations = check_file(file_path, brick_name=brick_name)
        if violations:
            all_violations.append((file_path, violations))

    if all_violations:
        print("\n" + "=" * 72)
        print("Brick import boundary check FAILED")
        print("=" * 72)
        print()

        for file_path, violations in all_violations:
            print(f"  {file_path}:")
            for line_num, line_content, desc in violations:
                print(f"    Line {line_num}: {line_content}")
                print(f"             -> Forbidden: direct import from {desc}")
            print()

        print("LEGO Architecture Principle 3: Bricks don't know about the kernel")
        print("or each other. Bricks may only import from:")
        print("     nexus.core.protocols.*      (kernel protocol interfaces)")
        print("     nexus.contracts.cache_store  (CacheStoreABC -- storage pillar)")
        print("     nexus.core.object_store     (ObjectStoreABC -- storage pillar)")
        print("     nexus.contracts.protocols.*  (service protocol interfaces)")
        print("     nexus.storage.*              (RecordStoreABC + storage utilities)")
        print("     nexus.bricks.<own_brick>.*   (same-brick internal imports)")
        print()
        print()

        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
