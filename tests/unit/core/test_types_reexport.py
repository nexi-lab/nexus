"""Leaf-module tests for contracts/types.py (Issue #1501).

Verifies that:
1. contracts/types.py has no runtime nexus.* imports outside its own package.
2. contracts/types.py does NOT use ``from __future__ import annotations``.
"""

import ast
from pathlib import Path

_CONTRACTS_TYPES_FILE = (
    Path(__file__).resolve().parents[3] / "src" / "nexus" / "contracts" / "types.py"
)


class TestTypesIsLeafModule:
    """contracts/types.py must have no external runtime nexus.* imports (Issue #1501)."""

    def test_no_runtime_nexus_imports(self) -> None:
        """AST check: contracts/types.py has no runtime imports from nexus.* outside its own package."""
        source = _CONTRACTS_TYPES_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(_CONTRACTS_TYPES_FILE))

        # Collect lines inside TYPE_CHECKING blocks
        tc_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test_node = node.test
                if (isinstance(test_node, ast.Name) and test_node.id == "TYPE_CHECKING") or (
                    isinstance(test_node, ast.Attribute) and test_node.attr == "TYPE_CHECKING"
                ):
                    for child in ast.walk(node):
                        if hasattr(child, "lineno"):
                            tc_lines.add(child.lineno)

        runtime_nexus_imports: list[str] = []
        for node in ast.iter_child_nodes(tree):
            if hasattr(node, "lineno") and node.lineno in tc_lines:
                continue

            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("nexus") and not alias.name.startswith(
                        "nexus.contracts."
                    ):
                        runtime_nexus_imports.append(f"import {alias.name}")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("nexus")
                # Allow intra-package imports (nexus.contracts.*)
                and not node.module.startswith("nexus.contracts.")
            ):
                names = ", ".join(a.name for a in node.names)
                runtime_nexus_imports.append(f"from {node.module} import {names}")

        assert runtime_nexus_imports == [], (
            f"contracts/types.py must be a leaf module with no external nexus imports, "
            f"but has runtime nexus imports: {runtime_nexus_imports}"
        )

    def test_no_future_annotations(self) -> None:
        """contracts/types.py must NOT use ``from __future__ import annotations``."""
        source = _CONTRACTS_TYPES_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(_CONTRACTS_TYPES_FILE))

        has_future = False
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                for alias in node.names:
                    if alias.name == "annotations":
                        has_future = True
        assert not has_future, (
            "contracts/types.py must NOT use 'from __future__ import annotations'"
        )
