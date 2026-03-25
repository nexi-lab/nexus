"""Packaging boundary test: nexus.fs must not import excluded modules.

Parses all Python files in src/nexus/fs/ using AST and asserts that no
import statement references modules excluded by packages/nexus-fs/pyproject.toml.

This catches the class of bug from Issue #3326 where monorepo imports
silently succeed but the published slim wheel would fail.
"""

from __future__ import annotations

import ast
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
