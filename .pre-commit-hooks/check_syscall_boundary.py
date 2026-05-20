#!/usr/bin/env python3
"""Pre-commit hook and CI check enforcing the §2.5 syscall mediation boundary.

KERNEL-ARCHITECTURE.md §2.5: service-tier code reaches kernel state ONLY
through the §2.2 syscall surface (sys_read / sys_write / sys_readdir / ...)
or the §4 dispatch hook ABI. The MetaStore (§3.A.1) and ObjectStore (§3.A.2)
pillars are kernel-internal HAL contracts — reaching them directly is a
boundary violation.

Service tier = the Python packages that sit ABOVE the kernel:
    src/nexus/bricks/      src/nexus/services/      src/nexus/server/

This check fails the build if any file under those trees reaches a HAL
pillar directly:

  * MetaStore pillar — metastore_list / metastore_list_iter /
    metastore_list_paginated (the recursive directory enumeration the
    kernel implements over). Service tier lists via sys_readdir instead.

  * ObjectStore pillar — <something>backend.read_content(...) /
    <something>backend.write_content(...) (hash-addressed blob I/O).
    Service tier reads/writes paths via sys_read / sys_write instead.

Kernel-internal callers (nexus.core, nexus.kernel_helpers, the CAS GC in
nexus.backends) are intentionally NOT scanned — they are the kernel /
wrapper layer and may use the pillars directly.

Reference: docs/architecture/KERNEL-ARCHITECTURE.md §2.5
"""

import ast
import sys
from pathlib import Path

# Service-tier roots, relative to the repository root.
SERVICE_TIER_ROOTS = (
    Path("src") / "nexus" / "bricks",
    Path("src") / "nexus" / "services",
    Path("src") / "nexus" / "server",
)

# MetaStore-pillar list helpers — forbidden as call targets or imports.
_METASTORE_LIST_NAMES = frozenset(
    {"metastore_list", "metastore_list_iter", "metastore_list_paginated"}
)
_METASTORE_DESC = "MetaStore pillar — use NexusFS.sys_readdir(recursive=True)"

# ObjectStore-pillar blob I/O — forbidden when called off a *backend* ref.
_OBJECTSTORE_METHODS = {
    "read_content": "ObjectStore pillar — use NexusFS.sys_read(path)",
    "write_content": "ObjectStore pillar — use NexusFS.sys_write(path, ...)",
}

# Module-level allowlist: dotted module name -> list of pillar descriptions
# tolerated in that module. Empty by design — the service tier is clean as
# of the service-tier-syscall-boundary cleanup. Add an entry ONLY with a
# tracking issue and a comment explaining the kernel-internal exception.
KNOWN_EXCEPTIONS: dict[str, list[str]] = {}


def _module_name_from_path(file_path: Path) -> str | None:
    """Derive dotted module name from a file path (best-effort)."""
    parts = file_path.parts
    try:
        src_idx = parts.index("src")
        mod_parts = list(parts[src_idx + 1 :])
    except ValueError:
        return None
    if mod_parts and mod_parts[-1].endswith(".py"):
        mod_parts[-1] = mod_parts[-1][:-3]
    if mod_parts and mod_parts[-1] == "__init__":
        mod_parts = mod_parts[:-1]
    return ".".join(mod_parts)


def _receiver_is_backend(node: ast.expr) -> bool:
    """True when an attribute access reads off a `*backend` reference.

    Matches backend.X / _backend.X / self.backend.X / route.backend.X —
    a blob op on an ObjectStore handle. Plain self.read_content (a class
    implementing the method itself) has no `backend` receiver and is not
    flagged.
    """
    if isinstance(node, ast.Name):
        return node.id.endswith("backend")
    if isinstance(node, ast.Attribute):
        return node.attr.endswith("backend")
    return False


class _BoundaryVisitor(ast.NodeVisitor):
    """Collects §2.5 HAL-pillar violations from a module AST."""

    def __init__(self) -> None:
        # (line, description)
        self.violations: list[tuple[int, str]] = []

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "nexus.kernel_helpers":
            for alias in node.names:
                if alias.name in _METASTORE_LIST_NAMES:
                    self.violations.append((node.lineno, _METASTORE_DESC))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        # Bare call: metastore_list_iter(kernel, ...)
        if isinstance(func, ast.Name) and func.id in _METASTORE_LIST_NAMES:
            self.violations.append((node.lineno, _METASTORE_DESC))
        elif isinstance(func, ast.Attribute):
            # Attribute call: kernel.metastore_list_paginated(...)
            if func.attr in _METASTORE_LIST_NAMES:
                self.violations.append((node.lineno, _METASTORE_DESC))
            # Attribute call: <...>backend.read_content(...) / write_content
            elif func.attr in _OBJECTSTORE_METHODS and _receiver_is_backend(func.value):
                self.violations.append((node.lineno, _OBJECTSTORE_METHODS[func.attr]))
        self.generic_visit(node)


def check_file(file_path: Path) -> list[tuple[int, str, str]]:
    """Return (line_number, line_content, description) for each violation."""
    module_name = _module_name_from_path(file_path) or ""
    allowed = KNOWN_EXCEPTIONS.get(module_name, [])
    try:
        source = file_path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: could not read {file_path}: {exc}")
        return []
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as exc:
        print(f"Warning: could not parse {file_path}: {exc}")
        return []

    visitor = _BoundaryVisitor()
    visitor.visit(tree)
    if not visitor.violations:
        return []

    lines = source.splitlines()
    out: list[tuple[int, str, str]] = []
    for line_num, desc in sorted(set(visitor.violations)):
        if desc in allowed:
            continue
        content = lines[line_num - 1].strip() if 0 < line_num <= len(lines) else ""
        out.append((line_num, content, desc))
    return out


def _is_service_tier(path: Path) -> bool:
    norm = str(path).replace("\\", "/")
    if "/tests/" in norm or norm.startswith("tests/"):
        return False
    return any(f"/{root.as_posix()}/" in f"/{norm}" for root in SERVICE_TIER_ROOTS)


def find_service_tier_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for rel_root in SERVICE_TIER_ROOTS:
        tier_dir = root / rel_root
        if tier_dir.exists():
            files.extend(p for p in tier_dir.rglob("*.py") if "/tests/" not in p.as_posix())
    return sorted(files)


def main() -> int:
    """Pre-commit mode (file args) or CI mode (scan all service-tier files)."""
    if len(sys.argv) > 1:
        files = [Path(f) for f in sys.argv[1:] if f.endswith(".py") and _is_service_tier(Path(f))]
    else:
        files = find_service_tier_files(Path.cwd())

    if not files:
        return 0

    all_violations: list[tuple[Path, list[tuple[int, str, str]]]] = []
    for file_path in files:
        violations = check_file(file_path)
        if violations:
            all_violations.append((file_path, violations))

    if not all_violations:
        return 0

    print("\n" + "=" * 72)
    print("Syscall boundary check FAILED (KERNEL-ARCHITECTURE.md §2.5)")
    print("=" * 72)
    print()
    for file_path, violations in all_violations:
        print(f"  {file_path}:")
        for line_num, line_content, desc in violations:
            print(f"    Line {line_num}: {line_content}")
            print(f"             -> {desc}")
        print()
    print("Service-tier code (bricks/, services/, server/) reaches kernel")
    print("state ONLY through the §2.2 syscall surface:")
    print("     directory listing  -> NexusFS.sys_readdir(recursive=True)")
    print("     blob read / write  -> NexusFS.sys_read / sys_write (paths)")
    print("MetaStore and ObjectStore are kernel-internal HAL pillars.")
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
