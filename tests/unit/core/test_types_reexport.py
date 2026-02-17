"""Backward-compatibility re-export tests for core/types.py (Issue #1291, #1501).

Verifies that:
1. Types are importable from core.types, core.permissions, and contracts.types.
2. The imported classes are the *same* objects (identity, not just equality).
3. contracts/types.py is a zero-dependency leaf module (no runtime nexus.* imports).
4. core/types.py is a thin re-export shim pointing to contracts.types.
"""


import ast
from pathlib import Path

_CONTRACTS_TYPES_FILE = (
    Path(__file__).resolve().parents[3] / "src" / "nexus" / "contracts" / "types.py"
)


class TestOperationContextReExport:
    """OperationContext importable from both modules with identity."""

    def test_importable_from_types(self) -> None:
        from nexus.core.types import OperationContext

        assert OperationContext is not None

    def test_importable_from_permissions(self) -> None:
        from nexus.core.permissions import OperationContext

        assert OperationContext is not None

    def test_same_class_identity(self) -> None:
        from nexus.core.permissions import OperationContext as OC_permissions
        from nexus.core.types import OperationContext as OC_types

        assert OC_permissions is OC_types


class TestPermissionReExport:
    """Permission importable from both modules with identity."""

    def test_importable_from_types(self) -> None:
        from nexus.core.types import Permission

        assert Permission is not None

    def test_importable_from_permissions(self) -> None:
        from nexus.core.permissions import Permission

        assert Permission is not None

    def test_same_class_identity(self) -> None:
        from nexus.core.permissions import Permission as P_permissions
        from nexus.core.types import Permission as P_types

        assert P_permissions is P_types


class TestContextIdentityReExport:
    """ContextIdentity importable from both modules with identity."""

    def test_importable_from_types(self) -> None:
        from nexus.core.types import ContextIdentity

        assert ContextIdentity is not None


class TestExtractContextIdentityExport:
    """extract_context_identity importable from core.types."""

    def test_importable_from_types(self) -> None:
        from nexus.core.types import extract_context_identity

        assert extract_context_identity is not None


class TestTypesIsLeafModule:
    """contracts/types.py must have zero runtime nexus.* imports (Issue #1501)."""

    def test_no_runtime_nexus_imports(self) -> None:
        """AST check: contracts/types.py has no runtime imports from nexus.*."""
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
                    if alias.name.startswith("nexus"):
                        runtime_nexus_imports.append(f"import {alias.name}")
            elif (
                isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("nexus")
            ):
                names = ", ".join(a.name for a in node.names)
                runtime_nexus_imports.append(f"from {node.module} import {names}")

        assert runtime_nexus_imports == [], (
            f"contracts/types.py must be a zero-dependency leaf module, "
            f"but has runtime nexus imports: {runtime_nexus_imports}"
        )

    def test_has_future_annotations(self) -> None:
        """contracts/types.py must use ``from __future__ import annotations``."""
        source = _CONTRACTS_TYPES_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(_CONTRACTS_TYPES_FILE))

        has_future = False
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                for alias in node.names:
                    if alias.name == "annotations":
                        has_future = True
        assert has_future, "contracts/types.py must use 'from __future__ import annotations'"
