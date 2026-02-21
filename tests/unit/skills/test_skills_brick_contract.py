"""Brick contract tests for the Skills module (Issue #1400).

These tests enforce architectural constraints:
1. Zero module-level runtime imports from nexus.core/nexus.backends in skills
2. Protocol satisfaction (NexusFS ABC satisfies narrow Skills Protocol)
3. Boundary contract enforcement (local exceptions, public API surface)
"""

import ast
from pathlib import Path

# Path to skills brick source (canonical location)
SKILLS_SRC = Path(__file__).resolve().parents[3] / "src" / "nexus" / "bricks" / "skills"


def _collect_import_violations(
    module_prefix: str,
    allowed_prefixes: list[str] | None = None,
) -> list[str]:
    """Scan skills/*.py for module-level runtime imports from the given prefix.

    Allows:
    - TYPE_CHECKING-guarded imports (type hints only)
    - Function-scoped imports (lazy loading pattern, e.g. inside try/except)
    - Imports matching any of *allowed_prefixes* (e.g. protocol interfaces)
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
                if allowed_prefixes and any(node.module.startswith(p) for p in allowed_prefixes):
                    continue
                violations.append(f"{py_file.name}:{node.lineno} — from {node.module} import ...")
    return violations


class TestZeroCoreImports:
    """Verify that nexus.bricks.skills has zero module-level runtime imports from nexus.core.

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
        """No file in nexus/skills/ imports from nexus.services at module level.

        Allowed: nexus.services.protocols.* (protocol interfaces per LEGO §3.3).
        """
        violations = _collect_import_violations(
            "nexus.services", allowed_prefixes=["nexus.services.protocols"]
        )
        assert violations == [], (
            f"Found {len(violations)} module-level runtime imports from nexus.services "
            f"in skills module:\n" + "\n".join(f"  {v}" for v in violations)
        )


class TestProtocolSatisfaction:
    """Verify that the narrow Skills filesystem protocol is satisfied."""

    def test_nexusfs_abc_satisfies_skills_protocol(self):
        """NexusFS ABC has all methods required by the narrow Skills Protocol."""
        import inspect

        from nexus.bricks.skills.protocols import NexusFilesystem as NexusFilesystemProtocol
        from nexus.core.filesystem import NexusFilesystem as NexusFilesystemABC

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
        """nexus.bricks.skills.exceptions defines required exception types."""
        from nexus.bricks.skills import exceptions

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
        from nexus.bricks.skills.exceptions import (
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
        from nexus.bricks.skills.exceptions import SkillPermissionDeniedError, SkillValidationError

        assert SkillValidationError.is_expected is True
        assert SkillPermissionDeniedError.is_expected is True


class TestModuleBoundary:
    """Verify the skills brick's public API boundary."""

    def test_skills_brick_exports_key_symbols(self):
        """nexus.bricks.skills submodules export the expected public symbols."""
        from nexus.bricks.skills.exporter import SkillExporter
        from nexus.bricks.skills.manager import SkillManager
        from nexus.bricks.skills.models import Skill, SkillMetadata
        from nexus.bricks.skills.parser import SkillParser
        from nexus.bricks.skills.registry import SkillRegistry

        for obj in [Skill, SkillMetadata, SkillParser, SkillRegistry, SkillManager, SkillExporter]:
            assert obj is not None

    def test_canonical_imports_work(self):
        """Canonical brick imports resolve correctly."""
        from nexus.bricks.skills.analytics import SkillAnalyticsTracker
        from nexus.bricks.skills.audit import SkillAuditLogger
        from nexus.bricks.skills.exporter import SkillExporter
        from nexus.bricks.skills.governance import SkillGovernance
        from nexus.bricks.skills.protocols import NexusFilesystem

        for obj in [
            SkillAnalyticsTracker,
            SkillGovernance,
            SkillAuditLogger,
            SkillExporter,
            NexusFilesystem,
        ]:
            assert obj is not None

    def test_skills_types_exported(self):
        """Skills service types are importable."""
        import dataclasses

        from nexus.bricks.skills.types import PromptContext, SkillContent, SkillInfo

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
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                if child is import_node:
                    return True
    return False
