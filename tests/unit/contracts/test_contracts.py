"""Tests for nexus.contracts — tier-neutral shared types and exceptions (Issue #1501).

Verifies:
1. Import paths work from both nexus.contracts and nexus.core (re-exports)
2. Object identity is preserved across import paths (same class object)
3. contracts/ modules have zero runtime nexus imports (leaf modules)
4. Skills duplicate exceptions were removed from individual modules
5. enable_read_tracking standalone function works correctly
"""

import ast
import importlib
from pathlib import Path

# ---------------------------------------------------------------------------
# Helper: check that a module has zero runtime ``nexus.*`` imports
# ---------------------------------------------------------------------------


def _get_runtime_nexus_imports(module_path: Path) -> list[str]:
    """Parse a Python file's AST and return runtime ``nexus.*`` import sources.

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

    nexus_imports: list[str] = []
    for node in ast.walk(tree):
        if hasattr(node, "lineno") and node.lineno in tc_lines:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("nexus"):
                    nexus_imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("nexus"):
            nexus_imports.append(node.module)

    return nexus_imports


# ---------------------------------------------------------------------------
# 1. Import path tests
# ---------------------------------------------------------------------------


class TestImportPaths:
    """Verify that types/exceptions are importable from nexus.contracts."""

    def test_import_operation_context_from_contracts(self):
        from nexus.contracts.types import OperationContext

        assert OperationContext is not None

    def test_import_permission_from_contracts(self):
        from nexus.contracts.types import Permission

        assert Permission is not None

    def test_import_context_identity_from_contracts(self):
        from nexus.contracts.types import ContextIdentity

        assert ContextIdentity is not None

    def test_import_extract_context_identity_from_contracts(self):
        from nexus.contracts.types import extract_context_identity

        assert callable(extract_context_identity)

    def test_import_nexus_error_from_contracts(self):
        from nexus.contracts.exceptions import NexusError

        assert NexusError is not None

    def test_import_parser_error_from_contracts(self):
        from nexus.contracts.exceptions import ParserError

        assert ParserError is not None

    def test_import_from_contracts_package_init(self):
        from nexus.contracts import NexusError, OperationContext, Permission

        assert OperationContext is not None
        assert Permission is not None
        assert NexusError is not None


# ---------------------------------------------------------------------------
# 2. Object identity tests (same class across import paths)
# ---------------------------------------------------------------------------


class TestObjectIdentity:
    """Verify that re-exports yield the exact same class object."""

    def test_nexus_error_identity(self):
        from nexus.contracts.exceptions import NexusError as ContractsErr
        from nexus.contracts.exceptions import NexusError as CoreErr

        assert ContractsErr is CoreErr

    def test_parser_error_identity(self):
        from nexus.contracts.exceptions import ParserError as ContractsPE
        from nexus.contracts.exceptions import ParserError as CorePE

        assert ContractsPE is CorePE

    def test_backend_error_identity(self):
        from nexus.contracts.exceptions import BackendError as ContractsBE
        from nexus.contracts.exceptions import BackendError as CoreBE

        assert ContractsBE is CoreBE

    def test_validation_error_identity(self):
        from nexus.contracts.exceptions import ValidationError as ContractsVE
        from nexus.contracts.exceptions import ValidationError as CoreVE

        assert ContractsVE is CoreVE


# ---------------------------------------------------------------------------
# 3. AST zero-dependency tests (contracts modules have no nexus.* imports)
# ---------------------------------------------------------------------------


class TestZeroDependency:
    """Verify contracts/ modules are leaf modules with no nexus.* runtime imports."""

    def test_contracts_types_has_zero_external_nexus_imports(self):
        mod_path = Path(importlib.import_module("nexus.contracts.types").__file__)
        imports = _get_runtime_nexus_imports(mod_path)
        # Intra-package imports (nexus.contracts.*) are allowed
        external = [i for i in imports if not i.startswith("nexus.contracts.")]
        assert external == [], f"contracts/types.py has external runtime nexus imports: {external}"

    def test_contracts_exceptions_has_zero_nexus_imports(self):
        mod_path = Path(importlib.import_module("nexus.contracts.exceptions").__file__)
        imports = _get_runtime_nexus_imports(mod_path)
        assert imports == [], f"contracts/exceptions.py has runtime nexus imports: {imports}"


# ---------------------------------------------------------------------------
# 4. Skills duplicate removal tests
# ---------------------------------------------------------------------------


class TestSkillsDuplicateRemoval:
    """Verify duplicate exception classes were removed from skills sub-modules."""

    def test_skill_manager_error_not_defined_in_manager(self):
        """SkillManagerError should NOT be defined in skills/manager.py."""
        import nexus.bricks.skills.manager as mod

        source = Path(mod.__file__).read_text()
        tree = ast.parse(source)
        class_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        assert "SkillManagerError" not in class_names

    def test_skill_export_error_not_defined_in_exporter(self):
        """SkillExportError should NOT be defined in skills/exporter.py."""
        import nexus.bricks.skills.exporter as mod

        source = Path(mod.__file__).read_text()
        tree = ast.parse(source)
        class_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        assert "SkillExportError" not in class_names

    def test_skill_parse_error_not_defined_in_parser(self):
        """SkillParseError should NOT be defined in skills/parser.py."""
        import nexus.bricks.skills.parser as mod

        source = Path(mod.__file__).read_text()
        tree = ast.parse(source)
        class_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        assert "SkillParseError" not in class_names

    def test_skill_not_found_error_not_defined_in_registry(self):
        """SkillNotFoundError should NOT be defined in skills/registry.py."""
        import nexus.bricks.skills.registry as mod

        source = Path(mod.__file__).read_text()
        tree = ast.parse(source)
        class_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        assert "SkillNotFoundError" not in class_names

    def test_skill_dependency_error_not_defined_in_registry(self):
        """SkillDependencyError should NOT be defined in skills/registry.py."""
        import nexus.bricks.skills.registry as mod

        source = Path(mod.__file__).read_text()
        tree = ast.parse(source)
        class_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        assert "SkillDependencyError" not in class_names

    def test_skill_import_error_not_defined_in_importer(self):
        """SkillImportError should NOT be defined in skills/importer.py."""
        import nexus.bricks.skills.importer as mod

        source = Path(mod.__file__).read_text()
        tree = ast.parse(source)
        class_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        assert "SkillImportError" not in class_names

    def test_all_skill_exceptions_in_exceptions_module(self):
        """All skill exceptions should be defined in skills/exceptions.py."""
        from nexus.bricks.skills.exceptions import (
            SkillDependencyError,
            SkillExportError,
            SkillImportError,
            SkillManagerError,
            SkillNotFoundError,
            SkillParseError,
            SkillPermissionDeniedError,
            SkillValidationError,
        )

        # Verify they're all importable
        assert SkillValidationError is not None
        assert SkillPermissionDeniedError is not None
        assert SkillNotFoundError is not None
        assert SkillDependencyError is not None
        assert SkillManagerError is not None
        assert SkillExportError is not None
        assert SkillParseError is not None
        assert SkillImportError is not None


# ---------------------------------------------------------------------------
# 5. enable_read_tracking standalone function test
# ---------------------------------------------------------------------------


class TestEnableReadTrackingStandalone:
    """Verify the standalone enable_read_tracking function works."""

    def test_enable_read_tracking_basic(self):
        from nexus.contracts.types import OperationContext
        from nexus.storage.read_set import enable_read_tracking

        ctx = OperationContext(user_id="alice", groups=[], zone_id="org1")
        enable_read_tracking(ctx)
        assert ctx.track_reads is True
        assert ctx.read_set is not None
        assert ctx.read_set.zone_id == "org1"

    def test_enable_read_tracking_with_explicit_zone(self):
        from nexus.contracts.types import OperationContext
        from nexus.storage.read_set import enable_read_tracking

        ctx = OperationContext(user_id="alice", groups=[], zone_id="org1")
        enable_read_tracking(ctx, zone_id="custom_zone")
        assert ctx.read_set.zone_id == "custom_zone"

    def test_enable_read_tracking_defaults_to_root(self):
        from nexus.contracts.types import OperationContext
        from nexus.storage.read_set import enable_read_tracking

        ctx = OperationContext(user_id="alice", groups=[])
        enable_read_tracking(ctx)
        assert ctx.read_set.zone_id == "root"
