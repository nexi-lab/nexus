"""Packaging boundary test: nexus.fs must not import excluded modules.

Two layers of defence:

1. **Static** — AST-parse all Python files in src/nexus/fs/ and assert that
   no import statement references modules excluded by
   packages/nexus-fs/pyproject.toml.

2. **Runtime** — import nexus.fs and exercise its public API, then verify
   that no excluded modules leaked into sys.modules. This catches the
   exact failure mode from Issue #3326: "works in the monorepo, fails
   once packaged."
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# Modules explicitly excluded in packages/nexus-fs/pyproject.toml [tool.hatch.build.targets.wheel]
EXCLUDED_MODULES = frozenset(
    {
        "nexus.bricks",
        "nexus.server",
        "nexus.factory",
        "nexus.raft",
        "nexus.cli",
        "nexus.fuse",
        "nexus.remote",
        "nexus.system_services",
        "nexus.grpc",
        "nexus.security",
    }
)

# Root of the nexus.fs package source
FS_PACKAGE_DIR = Path(__file__).resolve().parents[3] / "src" / "nexus" / "fs"


def _is_excluded(module_name: str) -> str | None:
    """Return the excluded root if module_name falls under an excluded tree, else None."""
    for excluded in EXCLUDED_MODULES:
        if module_name == excluded or module_name.startswith(excluded + "."):
            return excluded
    return None


def _collect_imports(filepath: Path) -> list[tuple[int, str]]:
    """Parse a Python file and return (line_number, module_name) for all imports."""
    source = filepath.read_text()
    tree = ast.parse(source, filename=str(filepath))
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.lineno, node.module))
    return imports


def _collect_violations() -> list[str]:
    """Scan all .py files in nexus.fs for imports from excluded modules."""
    violations: list[str] = []
    for py_file in sorted(FS_PACKAGE_DIR.rglob("*.py")):
        rel = py_file.relative_to(FS_PACKAGE_DIR.parent.parent)  # relative to src/
        for lineno, module in _collect_imports(py_file):
            excluded_root = _is_excluded(module)
            if excluded_root is not None:
                violations.append(f"{rel}:{lineno} imports '{module}' (excluded: {excluded_root})")
    return violations


class TestPackagingBoundary:
    def test_no_imports_from_excluded_modules(self):
        """nexus.fs source must not import from modules excluded by the slim wheel."""
        violations = _collect_violations()
        assert violations == [], (
            "nexus.fs imports from modules excluded in pyproject.toml:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    def test_excluded_list_matches_pyproject(self):
        """Verify our excluded list matches the actual pyproject.toml excludes."""
        import tomllib

        pyproject = FS_PACKAGE_DIR.parents[2] / "packages" / "nexus-fs" / "pyproject.toml"
        if not pyproject.exists():
            pytest.skip("pyproject.toml not found (running outside monorepo)")
        with open(pyproject, "rb") as f:
            config = tomllib.load(f)
        excludes = config["tool"]["hatch"]["build"]["targets"]["wheel"]["exclude"]
        # Convert glob patterns like "nexus/factory/**" to dotted module roots
        pyproject_excluded = set()
        for pattern in excludes:
            # "nexus/factory/**" → "nexus.factory"
            root = pattern.replace("/**", "").replace("/", ".")
            pyproject_excluded.add(root)
        assert pyproject_excluded == EXCLUDED_MODULES, (
            f"EXCLUDED_MODULES in test is out of sync with pyproject.toml.\n"
            f"  Test has: {sorted(EXCLUDED_MODULES - pyproject_excluded)}\n"
            f"  pyproject has: {sorted(pyproject_excluded - EXCLUDED_MODULES)}"
        )


# ---------------------------------------------------------------------------
# Runtime boundary test — in-process, no subprocess, xdist-safe
# ---------------------------------------------------------------------------


class TestRuntimeBoundary:
    """Import nexus.fs and verify no excluded modules leak into sys.modules.

    This is the runtime complement to the static AST test above. It catches
    cases where a lazy import or __getattr__ hook would pull in an excluded
    module at runtime even though the source-level import is clean.
    """

    def test_import_does_not_pull_excluded_modules(self):
        """Importing nexus.fs and accessing its public API must not load excluded modules."""
        # Snapshot which excluded modules are already loaded (e.g. by other tests)
        already_loaded = {mod for mod in sys.modules if _is_excluded(mod) is not None}

        # Exercise the public API surface that mount() uses
        import nexus.fs  # noqa: F811
        from nexus.fs._cli import main  # CLI entry point
        from nexus.fs._facade import SlimNexusFS  # noqa: F401
        from nexus.fs._uri import parse_uri  # noqa: F401

        # Access lazy attributes to trigger __getattr__
        _ = nexus.fs.SlimNexusFS
        _ = nexus.fs.parse_uri
        assert callable(nexus.fs.mount)

        # Run CLI --help via click.testing (no subprocess needed)
        from click.testing import CliRunner

        result = CliRunner().invoke(main, ["--help"])
        assert result.exit_code == 0

        # Check: no NEW excluded modules should have been loaded
        newly_loaded = {
            mod
            for mod in sys.modules
            if _is_excluded(mod) is not None and mod not in already_loaded
        }
        assert newly_loaded == set(), (
            "Importing nexus.fs pulled in excluded modules:\n"
            + "\n".join(f"  - {mod}" for mod in sorted(newly_loaded))
        )
