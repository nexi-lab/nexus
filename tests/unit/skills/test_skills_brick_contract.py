"""Brick contract tests for the Skills module (Issue #1400).

These tests enforce architectural constraints:
1. Zero module-level runtime imports from nexus.core/nexus.backends in skills
2. Protocol satisfaction (NexusFS ABC satisfies narrow Skills Protocol)
3. Boundary contract enforcement (local exceptions, public API surface)
"""

import ast
from pathlib import Path

import pytest

# Path to skills module source
SKILLS_SRC = Path(__file__).resolve().parents[3] / "src" / "nexus" / "skills"


def _collect_import_violations(module_prefix: str) -> list[str]:
    """Scan skills/*.py for module-level runtime imports from the given prefix.

    Allows:
    - TYPE_CHECKING-guarded imports (type hints only)
    - Function-scoped imports (lazy loading pattern, e.g. inside try/except)
    """
    violations = []
    for py_file in SKILLS_SRC.glob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith(module_prefix)
                and not _is_type_checking_guarded(tree, node)
                and not _is_function_scoped(tree, node)
            ):
                violations.append(f"{py_file.name}:{node.lineno} — from {node.module} import ...")
    return violations


class TestZeroCoreImports:
    """Verify that nexus.skills has zero module-level runtime imports from nexus.core.

    All nexus.core imports are replaced with local exceptions and Protocols.
    TYPE_CHECKING-guarded and function-scoped (lazy) imports are allowed.
    """

    def test_no_runtime_core_imports(self):
        """No file in nexus/skills/ imports from nexus.core at module level."""
        violations = _collect_import_violations("nexus.core")
        assert violations == [], (
            f"Found {len(violations)} module-level runtime imports from nexus.core "
            f"in skills module:\n" + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_runtime_backends_imports(self):
        """No file in nexus/skills/ imports from nexus.backends at module level."""
        violations = _collect_import_violations("nexus.backends")
        assert violations == [], (
            f"Found {len(violations)} module-level runtime imports from nexus.backends "
            f"in skills module:\n" + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_runtime_services_imports(self):
        """No file in nexus/skills/ imports from nexus.services at module level."""
        violations = _collect_import_violations("nexus.services")
        assert violations == [], (
            f"Found {len(violations)} module-level runtime imports from nexus.services "
            f"in skills module:\n" + "\n".join(f"  {v}" for v in violations)
        )


class TestProtocolSatisfaction:
    """Verify that the narrow Skills filesystem protocol is satisfied."""

    def test_nexusfs_abc_satisfies_skills_protocol(self):
        """NexusFS ABC has all methods required by the narrow Skills Protocol."""
        import inspect

        from nexus.core.filesystem import NexusFilesystem as NexusFilesystemABC
        from nexus.skills.protocols import NexusFilesystem as NexusFilesystemProtocol

        # Get method names from the narrow protocol
        protocol_methods = {
            name
            for name in dir(NexusFilesystemProtocol)
            if not name.startswith("_") and callable(getattr(NexusFilesystemProtocol, name, None))
        }

        # Get method names from the ABC
        abc_methods = {
            name
            for name, _ in inspect.getmembers(NexusFilesystemABC, predicate=inspect.isfunction)
            if not name.startswith("_")
        }

        missing = protocol_methods - abc_methods
        assert missing == set(), (
            f"NexusFilesystem ABC is missing methods required by Skills Protocol: {sorted(missing)}"
        )


class TestSkillsExceptionLocality:
    """Verify that skills module defines its own exception types."""

    def test_local_exceptions_exist(self):
        """nexus.skills.exceptions defines required exception types."""
        from nexus.skills import exceptions

        required = [
            "SkillValidationError",
            "SkillPermissionDeniedError",
            "SkillNotFoundError",
            "SkillDependencyError",
            "SkillManagerError",
            "SkillExportError",
            "SkillParseError",
        ]
        for name in required:
            assert hasattr(exceptions, name), f"Missing exception: {name}"
            cls = getattr(exceptions, name)
            assert issubclass(cls, Exception), f"{name} must be an Exception subclass"

    def test_exception_hierarchy(self):
        """SkillValidationError is the base for domain exceptions."""
        from nexus.skills.exceptions import (
            SkillDependencyError,
            SkillExportError,
            SkillManagerError,
            SkillNotFoundError,
            SkillParseError,
            SkillValidationError,
        )

        for cls in [
            SkillNotFoundError,
            SkillDependencyError,
            SkillManagerError,
            SkillExportError,
            SkillParseError,
        ]:
            assert issubclass(cls, SkillValidationError), (
                f"{cls.__name__} should be a subclass of SkillValidationError"
            )

    def test_exceptions_have_is_expected_attr(self):
        """Skill exceptions expose is_expected for error handling."""
        from nexus.skills.exceptions import SkillPermissionDeniedError, SkillValidationError

        assert SkillValidationError.is_expected is True
        assert SkillPermissionDeniedError.is_expected is True


class TestModuleBoundary:
    """Verify the skills module's public API boundary."""

    def test_skills_init_exports_key_symbols(self):
        """nexus.skills.__init__ exports the expected public symbols."""
        from nexus import skills

        required = [
            "Skill",
            "SkillMetadata",
            "SkillParser",
            "SkillRegistry",
            "SkillManager",
            "SkillExporter",
        ]
        for name in required:
            assert hasattr(skills, name), f"Missing export: nexus.skills.{name}"

    def test_lazy_imports_work(self):
        """Lazy imports via __getattr__ resolve correctly."""
        from nexus import skills

        # These are lazy — not eagerly imported
        lazy_names = [
            "SkillAnalyticsTracker",
            "SkillGovernance",
            "SkillAuditLogger",
            "SkillExporter",
            "NexusFilesystem",
        ]
        for name in lazy_names:
            obj = getattr(skills, name, None)
            assert obj is not None, f"Lazy import failed for nexus.skills.{name}"

    def test_invalid_attr_raises(self):
        """Accessing nonexistent attributes raises AttributeError."""
        from nexus import skills

        with pytest.raises(AttributeError, match="no attribute"):
            _ = skills.NonExistentSymbol  # type: ignore[attr-defined]

    def test_skills_types_exported(self):
        """Skills service types are importable."""
        import dataclasses

        from nexus.skills.types import PromptContext, SkillContent, SkillInfo

        # Verify they're dataclasses with expected fields
        si_fields = {f.name for f in dataclasses.fields(SkillInfo)}
        assert "path" in si_fields
        assert "name" in si_fields
        sc_fields = {f.name for f in dataclasses.fields(SkillContent)}
        assert "content" in sc_fields
        pc_fields = {f.name for f in dataclasses.fields(PromptContext)}
        assert "xml" in pc_fields


# =============================================================================
# AST Helpers: detect guarded / scoped imports
# =============================================================================


def _is_type_checking_guarded(tree: ast.Module, import_node: ast.ImportFrom) -> bool:
    """Check if an import node is inside an `if TYPE_CHECKING:` block."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_tc = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute)
            and isinstance(test.value, ast.Name)
            and test.value.id == "typing"
            and test.attr == "TYPE_CHECKING"
        )
        if is_tc:
            for child in ast.walk(node):
                if child is import_node:
                    return True
    return False


def _is_function_scoped(tree: ast.Module, import_node: ast.ImportFrom) -> bool:
    """Check if an import node is inside a function or method body."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if child is import_node:
                    return True
    return False
