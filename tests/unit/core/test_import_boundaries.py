"""Tests for architecture import boundary enforcement.

Issue #1519, 11A: Uses ast.parse() to verify that kernel modules (core/)
do NOT import from server/ or other forbidden layers. This prevents
architecture violations from creeping back in.

Tier hierarchy (Liedtke minimality):
    Storage Pillars → Kernel (core/) → System Services (services/) → Bricks
    - core/ must NOT import from server/
    - core/ must NOT import from services/ at top level (lazy OK in _wire_services)
    - services/ must NOT import from server/ (except via protocols)
"""

from __future__ import annotations

import ast
from pathlib import Path

# Project root for src/nexus/
NEXUS_ROOT = Path(__file__).resolve().parents[3] / "src" / "nexus"


def _collect_imports(module_path: Path) -> list[tuple[str, int, str]]:
    """Parse a Python file and return all import targets with line numbers.

    Returns list of (module_name, line_number, import_type) tuples.
    import_type is 'import' or 'from'.
    """
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))
    imports: list[tuple[str, int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, node.lineno, "import"))
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.module, node.lineno, "from"))

    return imports


def _collect_top_level_imports(module_path: Path) -> list[tuple[str, int, str]]:
    """Parse a Python file and return only TOP-LEVEL imports.

    Excludes imports inside functions/methods (lazy imports are OK).
    """
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))
    imports: list[tuple[str, int, str]] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, node.lineno, "import"))
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.module, node.lineno, "from"))
        # Also check TYPE_CHECKING blocks at top level
        elif isinstance(node, ast.If):
            # Check for `if TYPE_CHECKING:` pattern
            test = node.test
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.ImportFrom) and sub.module:
                        imports.append((sub.module, sub.lineno, "from"))
                    elif isinstance(sub, ast.Import):
                        for alias in sub.names:
                            imports.append((alias.name, sub.lineno, "import"))

    return imports


def _get_python_files(directory: Path) -> list[Path]:
    """Get all .py files in directory (non-recursive for top-level modules)."""
    return sorted(directory.glob("*.py"))


def _get_python_files_recursive(directory: Path) -> list[Path]:
    """Get all .py files recursively."""
    return sorted(directory.rglob("*.py"))


class TestKernelDoesNotImportServer:
    """Verify core/ modules never import from server/ at any level."""

    def test_no_server_imports_in_core(self):
        """No core/ module should import from nexus.server (any level)."""
        core_dir = NEXUS_ROOT / "core"
        violations: list[str] = []

        for py_file in _get_python_files_recursive(core_dir):
            rel = py_file.relative_to(NEXUS_ROOT)
            for module, lineno, _kind in _collect_imports(py_file):
                if module.startswith("nexus.server"):
                    violations.append(f"{rel}:{lineno} imports {module}")

        assert violations == [], "Kernel→Server import violations found:\n" + "\n".join(
            f"  - {v}" for v in violations
        )


class TestKernelTopLevelImports:
    """Verify core/ top-level imports don't pull in services/."""

    # Pre-existing violations that are tracked for cleanup (Issue #1519)
    KNOWN_CORE_SERVICES_IMPORTS = {
        "core/async_bridge.py",  # async_rebac_manager (TYPE_CHECKING)
        "core/async_nexus_fs.py",  # async_permissions (TYPE_CHECKING)
        "core/nexus_fs.py",  # memory_api, entity_registry (TYPE_CHECKING)
        "core/config.py",  # namespace_manager (TYPE_CHECKING)
    }

    def test_no_top_level_services_imports_in_core_modules(self):
        """Core modules should not have top-level imports from services/.

        Lazy imports inside methods (e.g., _wire_services) are allowed.
        Known exceptions are tracked for future cleanup.
        """
        core_dir = NEXUS_ROOT / "core"
        violations: list[str] = []

        for py_file in _get_python_files(core_dir):
            if py_file.name == "__init__.py":
                continue
            rel = str(py_file.relative_to(NEXUS_ROOT))
            if rel in self.KNOWN_CORE_SERVICES_IMPORTS:
                continue
            for module, lineno, _kind in _collect_top_level_imports(py_file):
                if module.startswith("nexus.services"):
                    violations.append(f"{rel}:{lineno} top-level imports {module}")

        assert violations == [], "Kernel→Services top-level import violations:\n" + "\n".join(
            f"  - {v}" for v in violations
        )


class TestServicesDoNotImportServer:
    """Verify services/ modules don't import from server/ (except via protocols)."""

    # Known exceptions: lazy imports for backward compat that are being cleaned up
    KNOWN_EXCEPTIONS = {
        # These are in the process of being migrated (Issue #1519)
        "services/oauth_service.py",
    }

    def test_no_top_level_server_imports_in_services(self):
        """Services should not have top-level imports from server/."""
        services_dir = NEXUS_ROOT / "services"
        violations: list[str] = []

        for py_file in _get_python_files_recursive(services_dir):
            rel = str(py_file.relative_to(NEXUS_ROOT))
            if rel in self.KNOWN_EXCEPTIONS:
                continue

            for module, lineno, _kind in _collect_top_level_imports(py_file):
                if module.startswith("nexus.server"):
                    violations.append(f"{rel}:{lineno} top-level imports {module}")

        assert violations == [], "Services→Server top-level import violations:\n" + "\n".join(
            f"  - {v}" for v in violations
        )


class TestRPCTypesInCore:
    """Verify RPC types are importable from core (Issue #1519, 1A)."""

    def test_rpc_types_importable_from_core(self):
        from nexus.core.rpc_types import RPCErrorCode, RPCRequest, RPCResponse

        assert RPCErrorCode.PARSE_ERROR.value == -32700
        assert RPCRequest().jsonrpc == "2.0"
        assert RPCResponse.success(1, "ok").result == "ok"

    def test_rpc_types_re_exported_from_server_protocol(self):
        from nexus.core.rpc_types import RPCErrorCode as CoreCode
        from nexus.server.protocol import RPCErrorCode as ServerCode

        assert CoreCode is ServerCode


class TestZoneHelpersInCore:
    """Verify zone helpers are importable from core (Issue #1519, 3A)."""

    def test_zone_helpers_importable_from_core(self):
        from nexus.core.zone_helpers import zone_group_id

        assert zone_group_id("acme") == "zone-acme"

    def test_zone_helpers_re_exported_from_server(self):
        from nexus.core.zone_helpers import is_zone_admin as CoreFn
        from nexus.server.auth.user_helpers import is_zone_admin as ServerFn

        assert CoreFn is ServerFn
