"""AST-based zero-core-import verification for parsers/ files (Issue #1523).

Verifies that all ``nexus/parsers/*.py`` files have zero runtime imports from
``nexus.core``, matching the tier-neutral brick pattern.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path


def _get_runtime_nexus_core_imports(module_path: Path) -> list[str]:
    """Parse a Python file's AST and return runtime ``nexus.core.*`` import sources.

    Skips imports inside ``if TYPE_CHECKING:`` blocks.
    """
    source = module_path.read_text()
    tree = ast.parse(source, filename=str(module_path))

    # Collect line ranges inside TYPE_CHECKING blocks
    tc_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            is_type_checking = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            )
            if is_type_checking:
                for child in ast.walk(node):
                    if hasattr(child, "lineno"):
                        tc_lines.add(child.lineno)

    core_imports: list[str] = []
    for node in ast.walk(tree):
        if hasattr(node, "lineno") and node.lineno in tc_lines:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("nexus.core"):
                    core_imports.append(alias.name)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("nexus.core")
        ):
            core_imports.append(node.module)

    return core_imports


class TestParsersZeroCoreImports:
    """Verify all parsers/ Python files have zero runtime nexus.core imports."""

    def _get_parsers_files(self) -> list[Path]:
        """Get all .py files under nexus/parsers/."""
        pkg = importlib.import_module("nexus.parsers")
        pkg_dir = Path(pkg.__file__).parent
        return sorted(pkg_dir.rglob("*.py"))

    def test_all_parsers_files_zero_core_imports(self) -> None:
        files = self._get_parsers_files()
        violations: list[str] = []
        for f in files:
            imports = _get_runtime_nexus_core_imports(f)
            if imports:
                violations.append(f"{f.name}: {imports}")

        assert violations == [], "parsers/ files with runtime nexus.core imports:\n" + "\n".join(
            f"  - {v}" for v in violations
        )
