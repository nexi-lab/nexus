"""Brick isolation tests for the search module (Issue #1520).

Validates that the search brick has no forbidden runtime imports from
core/, services/, or backends/ — enforcing LEGO Architecture boundaries.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# Path to search module source
SEARCH_PKG = Path(__file__).resolve().parents[3] / "src" / "nexus" / "search"

# Forbidden import prefixes (search brick must not depend on these)
FORBIDDEN_PREFIXES = (
    "nexus.core.",
    "nexus.services.",
    "nexus.backends.",
    "nexus.server.",
)

# Allowed exceptions: storage models are shared infrastructure (Storage Pillar)
ALLOWED_EXCEPTIONS = {
    "nexus.storage.models",
    "nexus.storage.content_cache",
}


def _collect_runtime_imports(filepath: Path) -> list[tuple[str, int, bool]]:
    """Parse a Python file and collect all runtime imports.

    Returns list of (module_name, line_number, is_type_checking).
    """
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))

    imports: list[tuple[str, int, bool]] = []
    type_checking_ranges: list[tuple[int, int]] = []

    # First pass: find TYPE_CHECKING blocks
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            is_tc = False
            if (
                isinstance(test, ast.Name)
                and test.id == "TYPE_CHECKING"
                or isinstance(test, ast.Attribute)
                and test.attr == "TYPE_CHECKING"
            ):
                is_tc = True
            if is_tc:
                start = node.lineno
                end = max(getattr(n, "lineno", start) for n in ast.walk(node))
                type_checking_ranges.append((start, end))

    # Second pass: collect imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                in_tc = any(s <= node.lineno <= e for s, e in type_checking_ranges)
                imports.append((alias.name, node.lineno, in_tc))
        elif isinstance(node, ast.ImportFrom) and node.module:
            in_tc = any(s <= node.lineno <= e for s, e in type_checking_ranges)
            imports.append((node.module, node.lineno, in_tc))

    return imports


class TestBrickIsolation:
    """Test that the search brick respects LEGO Architecture boundaries."""

    def test_no_forbidden_runtime_imports(self) -> None:
        """Search brick must not have runtime imports from core/services/backends."""
        violations: list[str] = []

        for py_file in sorted(SEARCH_PKG.glob("*.py")):
            if py_file.name.startswith("_") and py_file.name != "__init__.py":
                continue

            for module, line, is_tc in _collect_runtime_imports(py_file):
                if is_tc:
                    continue  # TYPE_CHECKING imports are fine
                if any(module.startswith(prefix) for prefix in FORBIDDEN_PREFIXES) and not any(
                    module.startswith(exc) for exc in ALLOWED_EXCEPTIONS
                ):
                    violations.append(f"{py_file.name}:{line} imports {module!r} at runtime")

        if violations:
            msg = "Search brick has forbidden runtime imports:\n" + "\n".join(
                f"  - {v}" for v in violations
            )
            pytest.fail(msg)

    def test_expected_files_exist(self) -> None:
        """Verify key search brick files exist."""
        expected = [
            "semantic.py",
            "async_search.py",
            "fusion.py",
            "chunking.py",
            "embeddings.py",
            "vector_db.py",
            "results.py",
            "strategies.py",
            "manifest.py",
        ]
        for name in expected:
            assert (SEARCH_PKG / name).exists(), f"Missing search brick file: {name}"

    def test_init_exports_base_result(self) -> None:
        """__init__.py must export BaseSearchResult and verify_imports."""
        init_file = SEARCH_PKG / "__init__.py"
        content = init_file.read_text(encoding="utf-8")
        assert "BaseSearchResult" in content
        assert "verify_imports" in content

    @pytest.mark.skipif(
        "nexus" not in sys.modules and not (SEARCH_PKG / "__init__.py").exists(),
        reason="nexus package not installed",
    )
    def test_verify_imports_returns_dict(self) -> None:
        """verify_imports() must return a dict of module -> bool."""
        try:
            from nexus.search.manifest import verify_imports

            result = verify_imports()
            assert isinstance(result, dict)
            assert len(result) > 0
            for key, val in result.items():
                assert isinstance(key, str)
                assert isinstance(val, bool)
        except ImportError:
            pytest.skip("nexus package not importable")
