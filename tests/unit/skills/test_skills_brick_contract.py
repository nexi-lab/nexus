"""Brick contract tests for the Skills module (Issue #1400).

These tests enforce architectural constraints:
1. Zero runtime imports from nexus.core in the skills module
2. Protocol satisfaction (NexusFS satisfies SkillsFilesystemProtocol)
3. Boundary contract enforcement

Phase 0: Skeleton tests — some will fail initially and pass after Phase 1.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Path to skills module source
SKILLS_SRC = Path(__file__).resolve().parents[3] / "src" / "nexus" / "skills"


def _collect_import_violations(module_prefix: str) -> list[str]:
    """Scan skills/*.py for runtime imports from the given module prefix."""
    violations = []
    for py_file in SKILLS_SRC.glob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith(module_prefix)
                and not _is_type_checking_guarded(tree, node)
            ):
                violations.append(f"{py_file.name}:{node.lineno} — from {node.module} import ...")
    return violations


class TestZeroCoreImports:
    """Verify that nexus.skills has zero runtime imports from nexus.core.

    After Phase 1, all nexus.core imports should be replaced with local
    exceptions and Protocols defined within the skills module.

    NOTE: TYPE_CHECKING-guarded imports are allowed (for type hints only).
    """

    @pytest.mark.skip(reason="Phase 1 not yet complete — will enforce after decoupling")
    def test_no_runtime_core_imports(self):
        """No file in nexus/skills/ imports from nexus.core at runtime."""
        violations = _collect_import_violations("nexus.core")
        assert violations == [], (
            f"Found {len(violations)} runtime imports from nexus.core "
            f"in skills module:\n" + "\n".join(f"  {v}" for v in violations)
        )

    @pytest.mark.skip(reason="Phase 1 not yet complete — will enforce after decoupling")
    def test_no_runtime_backends_imports(self):
        """No file in nexus/skills/ imports from nexus.backends at runtime."""
        violations = _collect_import_violations("nexus.backends")
        assert violations == [], (
            f"Found {len(violations)} runtime imports from nexus.backends "
            f"in skills module:\n" + "\n".join(f"  {v}" for v in violations)
        )


class TestProtocolSatisfaction:
    """Verify that protocols are satisfied by concrete implementations.

    After Phase 1, SkillsFilesystemProtocol should be narrow (~8 methods)
    and satisfied by NexusFS.
    """

    @pytest.mark.skip(reason="Phase 1 not yet complete — SkillsFilesystemProtocol not yet created")
    def test_nexusfs_satisfies_skills_filesystem_protocol(self):
        """NexusFS ABC satisfies the narrow SkillsFilesystemProtocol."""
        from nexus.core.filesystem import NexusFilesystem as NexusFilesystemABC
        from nexus.skills.protocols import SkillsFilesystemProtocol

        # Get method names from the narrow protocol
        proto_methods = {
            name
            for name, _ in SkillsFilesystemProtocol.__protocol_attrs__  # type: ignore[attr-defined]
            if not name.startswith("_")
        }

        # Get method names from the ABC
        abc_methods = {
            name
            for name in dir(NexusFilesystemABC)
            if not name.startswith("_") and callable(getattr(NexusFilesystemABC, name, None))
        }

        missing = proto_methods - abc_methods
        assert missing == set(), (
            f"NexusFilesystem ABC is missing methods required by "
            f"SkillsFilesystemProtocol: {missing}"
        )


class TestSkillsExceptionLocality:
    """Verify that skills module defines its own exception types.

    After Phase 1, all exception classes used by skills should be
    defined in nexus.skills.exceptions.
    """

    @pytest.mark.skip(reason="Phase 1 not yet complete — exceptions.py not yet created")
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
# Helper: detect TYPE_CHECKING-guarded imports
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
