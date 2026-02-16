"""Backward-compatibility re-export tests for core/types.py (Issue #1291, Decision 10A).

Verifies that:
1. Types are importable from both new (core.types) and old (core.permissions) locations.
2. The imported classes are the *same* objects (identity, not just equality).
3. core/types.py is a zero-dependency leaf module (no runtime nexus.* imports).
"""

from __future__ import annotations

import ast
from pathlib import Path

_TYPES_FILE = Path(__file__).resolve().parents[3] / "src" / "nexus" / "core" / "types.py"


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

    def test_importable_from_subsystem(self) -> None:
        from nexus.services.subsystem import ContextIdentity

        assert ContextIdentity is not None

    def test_same_class_identity(self) -> None:
        from nexus.core.types import ContextIdentity as CI_types
        from nexus.services.subsystem import ContextIdentity as CI_sub

        assert CI_types is CI_sub


class TestExtractContextIdentityReExport:
    """extract_context_identity importable from both modules."""

    def test_importable_from_types(self) -> None:
        from nexus.core.types import extract_context_identity

        assert extract_context_identity is not None

    def test_importable_from_subsystem(self) -> None:
        from nexus.services.subsystem import extract_context_identity

        assert extract_context_identity is not None

    def test_same_function_identity(self) -> None:
        from nexus.core.types import extract_context_identity as eci_types
        from nexus.services.subsystem import extract_context_identity as eci_sub

        assert eci_types is eci_sub


class TestTypesIsLeafModule:
    """core/types.py must have zero runtime nexus.* imports."""

    def test_no_runtime_nexus_imports(self) -> None:
        """AST check: types.py has no runtime imports from nexus.*."""
        source = _TYPES_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(_TYPES_FILE))

        runtime_nexus_imports: list[str] = []

        for node in ast.iter_child_nodes(tree):
            # Skip TYPE_CHECKING blocks
            if isinstance(node, ast.If):
                test = node.test
                if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                    isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
                ):
                    continue

            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("nexus"):
                        runtime_nexus_imports.append(f"import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("nexus"):
                names = ", ".join(a.name for a in node.names)
                runtime_nexus_imports.append(f"from {node.module} import {names}")

        assert runtime_nexus_imports == [], (
            f"core/types.py must be a zero-dependency leaf module, "
            f"but has runtime nexus imports: {runtime_nexus_imports}"
        )

    def test_has_future_annotations(self) -> None:
        """types.py must use ``from __future__ import annotations``."""
        source = _TYPES_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(_TYPES_FILE))

        has_future = False
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                for alias in node.names:
                    if alias.name == "annotations":
                        has_future = True
        assert has_future, "core/types.py must use 'from __future__ import annotations'"
