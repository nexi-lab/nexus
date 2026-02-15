"""AST-based brick isolation guard for nexus.a2a.

Ensures the A2A brick has zero runtime imports from ``nexus.server``
or ``nexus.core``.  The only accepted external coupling is
``nexus.storage.models.Base`` inside ``db.py`` (documented).
"""

from __future__ import annotations

import ast
from pathlib import Path

import nexus.a2a as _a2a_pkg

# Root of the nexus.a2a package
A2A_PKG = Path(_a2a_pkg.__file__).resolve().parent

# Forbidden top-level prefixes
FORBIDDEN_PREFIXES = ("nexus.server", "nexus.core")

# Allowed exceptions (module basename -> allowed import prefix)
ALLOWED_EXCEPTIONS: dict[str, set[str]] = {
    # db.py may import nexus.storage.models for SQLAlchemy Base (accepted coupling)
    "db.py": {"nexus.storage"},
    # agent_card.py and router.py use centralized URL defaults (#1462)
    "agent_card.py": {"nexus.constants"},
    "router.py": {"nexus.constants"},
}


class TestBrickIsolation:
    """Verify that nexus.a2a has no forbidden external imports."""

    def test_no_forbidden_imports(self) -> None:
        """Walk all .py files in nexus.a2a/ and assert no forbidden imports."""
        violations: list[str] = []

        for py_file in sorted(A2A_PKG.rglob("*.py")):
            source = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue

            rel = py_file.relative_to(A2A_PKG)
            _check_file(tree, str(rel), violations)

        assert violations == [], "Forbidden imports found in nexus.a2a:\n" + "\n".join(violations)

    def test_no_nexus_server_import(self) -> None:
        """Specifically verify zero nexus.server imports (the key coupling)."""
        violations: list[str] = []

        for py_file in sorted(A2A_PKG.rglob("*.py")):
            source = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue

            rel = py_file.relative_to(A2A_PKG)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.startswith("nexus.server")
                    and not _is_type_checking_import(tree, node)
                ):
                    violations.append(f"  {rel}:{node.lineno} — from {node.module} import ...")

        assert violations == [], "nexus.server imports found in nexus.a2a:\n" + "\n".join(
            violations
        )


def _check_file(tree: ast.Module, rel_path: str, violations: list[str]) -> None:
    """Check a single AST for forbidden imports."""
    basename = rel_path.split("/")[-1]
    allowed = ALLOWED_EXCEPTIONS.get(basename, set())

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue

        module = node.module

        # Skip intra-package imports
        if not module.startswith("nexus."):
            continue

        # Skip nexus.a2a internal imports
        if module.startswith("nexus.a2a"):
            continue

        # Check forbidden prefixes
        for prefix in FORBIDDEN_PREFIXES:
            if module.startswith(prefix):
                if not _is_type_checking_import(tree, node):
                    violations.append(f"  {rel_path}:{node.lineno} — from {module} import ...")
                break
        else:
            # Not a forbidden prefix — check if it's an allowed exception
            is_allowed = any(module.startswith(a) for a in allowed)
            if not is_allowed and not _is_type_checking_import(tree, node):
                # External nexus import not in allowed list
                violations.append(
                    f"  {rel_path}:{node.lineno} — from {module} import ... "
                    f"(not in allowed exceptions for {basename})"
                )


def _is_type_checking_import(tree: ast.Module, node: ast.ImportFrom) -> bool:
    """Check if an import is inside a ``if TYPE_CHECKING:`` block."""
    for top_node in ast.walk(tree):
        if isinstance(top_node, ast.If):
            # Check for `if TYPE_CHECKING:` pattern
            test = top_node.test
            is_tc = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            )
            if is_tc and node in ast.walk(top_node):
                return True
    return False
