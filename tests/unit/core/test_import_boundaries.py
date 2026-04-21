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

import ast
from pathlib import Path

import pytest

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
        "core/config.py",  # NamespaceManagerProtocol, namespace_manager (TYPE_CHECKING)
        "core/nexus_fs.py",  # memory_api, entity_registry (TYPE_CHECKING)
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

    def test_no_top_level_server_imports_in_services(self):
        """Services should not have top-level imports from server/."""
        services_dir = NEXUS_ROOT / "services"
        violations: list[str] = []

        for py_file in _get_python_files_recursive(services_dir):
            rel = str(py_file.relative_to(NEXUS_ROOT))
            for module, lineno, _kind in _collect_top_level_imports(py_file):
                if module.startswith("nexus.server"):
                    violations.append(f"{rel}:{lineno} top-level imports {module}")

        assert violations == [], "Services→Server top-level import violations:\n" + "\n".join(
            f"  - {v}" for v in violations
        )


class TestRPCTypesInCore:
    """Verify RPC types are importable from core (Issue #1519, 1A)."""

    def test_rpc_types_importable_from_contracts(self):
        from nexus.contracts.rpc_types import RPCErrorCode, RPCRequest, RPCResponse

        assert RPCErrorCode.PARSE_ERROR.value == -32700
        assert RPCRequest().jsonrpc == "2.0"
        assert RPCResponse.success(1, "ok").result == "ok"

    def test_rpc_types_re_exported_from_server_protocol(self):
        from nexus.contracts.rpc_types import RPCErrorCode as CoreCode
        from nexus.server.protocol import RPCErrorCode as ServerCode

        assert CoreCode is ServerCode


class TestFourStoragePillars:
    """Verify all Four Storage Pillars are importable ABCs (Issue #1525).

    The NEXUS-LEGO-ARCHITECTURE defines exactly four storage pillars:
    1. MetastoreABC   — inode/path metadata (Raft, redb)
    2. Backend         — object/blob storage (ObjectStoreABC: Local, GCS, S3)
    3. RecordStoreABC — relational data (PostgreSQL, SQLite)
    4. CacheStoreABC  — ephemeral KV + PubSub (Dragonfly, in-memory)
    """

    PILLARS = [
        ("nexus.core.metastore", "MetastoreABC"),
        ("nexus.backends.base.backend", "Backend"),
        ("nexus.storage.record_store", "RecordStoreABC"),
        ("nexus.contracts.cache_store", "CacheStoreABC"),
    ]

    @pytest.mark.parametrize(
        ("module_path", "class_name"),
        PILLARS,
        ids=["MetastoreABC", "Backend", "RecordStoreABC", "CacheStoreABC"],
    )
    def test_pillar_is_importable_abc(self, module_path: str, class_name: str):
        """Each storage pillar must be importable and be an ABC."""
        import importlib
        from abc import ABC

        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        assert isinstance(cls, type), f"{class_name} is not a class"
        assert issubclass(cls, ABC), f"{class_name} is not an ABC"

    def test_metastore_has_required_abstract_methods(self):
        """MetastoreABC must declare the required abstract methods.

        Public get/put/delete/exists/list are concrete (dcache layer).
        Subclasses implement _get_raw/_put_raw/_delete_raw/_exists_raw/_list_raw.
        """
        from nexus.core.metastore import MetastoreABC

        required = {"_get_raw", "_put_raw", "_delete_raw", "_exists_raw", "_list_raw", "close"}
        abstract = getattr(MetastoreABC, "__abstractmethods__", frozenset())
        missing = required - abstract
        assert not missing, f"MetastoreABC missing abstract methods: {missing}"

    def test_metastore_implementations_exist(self):
        """R20.18.5 renames the in-tree MetastoreABC implementer: the
        legacy ``RaftMetadataStore`` class became a deprecation shim;
        ``RustMetastoreProxy`` is the real implementation backing both
        ``.embedded()`` and every kernel-wired mount."""
        from nexus.core.metastore import MetastoreABC, RustMetastoreProxy

        assert issubclass(RustMetastoreProxy, MetastoreABC)

    def test_no_old_name_in_codebase(self):
        """FileMetadataProtocol should not appear in src/ (clean rename)."""
        import ast

        for py_file in sorted((NEXUS_ROOT).rglob("*.py")):
            source = py_file.read_text(encoding="utf-8")
            if "FileMetadataProtocol" in source:
                # Verify it's not in a string/comment — check ast
                tree = ast.parse(source, filename=str(py_file))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name) and node.id == "FileMetadataProtocol":
                        rel = py_file.relative_to(NEXUS_ROOT)
                        raise AssertionError(
                            f"Old name 'FileMetadataProtocol' found in {rel}:{node.lineno}"
                        )


class TestConfigDoesNotImportServer:
    """Verify nexus/config.py does not import from nexus.server (Issue #1389).

    The OAuthConfig models were moved to nexus.auth_config so that config.py
    can use them without reaching into the server layer.
    """

    def test_config_no_server_imports(self):
        """nexus/config.py must not import from nexus.server at any level."""
        config_path = NEXUS_ROOT / "config.py"
        violations: list[str] = []

        for module, lineno, _kind in _collect_imports(config_path):
            if module.startswith("nexus.server"):
                violations.append(f"config.py:{lineno} imports {module}")

        assert violations == [], "config.py→server import violations found:\n" + "\n".join(
            f"  - {v}" for v in violations
        )

    def test_auth_config_canonical_import(self):
        """OAuthConfig canonical path is nexus.contracts.oauth_types (#3230)."""
        from nexus.contracts.oauth_types import OAuthConfig, OAuthProviderConfig

        assert OAuthConfig is not None
        assert OAuthProviderConfig is not None

    def test_auth_config_backward_compat_import(self):
        """OAuthConfig backward-compat shim from bricks.auth.oauth.config (#3230)."""
        from nexus.bricks.auth.oauth.config import OAuthConfig as ShimOAuth
        from nexus.bricks.auth.oauth.config import OAuthProviderConfig as ShimProvider
        from nexus.contracts.oauth_types import OAuthConfig, OAuthProviderConfig

        assert ShimOAuth is OAuthConfig
        assert ShimProvider is OAuthProviderConfig

    def test_config_no_bricks_auth_imports(self):
        """nexus/config.py must not import from nexus.bricks.auth at top level (#3230).

        This prevents config from pulling in the auth brick, which may
        not be installed in the slim nexus-fs package.
        """
        config_path = NEXUS_ROOT / "config.py"
        violations: list[str] = []

        for module, lineno, _kind in _collect_top_level_imports(config_path):
            if module.startswith("nexus.bricks.auth"):
                violations.append(f"config.py:{lineno} imports {module}")

        assert violations == [], (
            "config.py→bricks.auth import violations found (#3230):\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    def test_sdk_no_bricks_rebac_top_level_imports(self):
        """nexus/sdk/__init__.py must not top-level import from nexus.bricks.rebac (#3230).

        ReBAC implementation types should be lazy-loaded via __getattr__ so
        that `import nexus.sdk` works without bricks.rebac installed.
        """
        sdk_init = NEXUS_ROOT / "sdk" / "__init__.py"
        violations: list[str] = []

        for module, lineno, _kind in _collect_top_level_imports(sdk_init):
            if module.startswith("nexus.bricks.rebac"):
                violations.append(f"sdk/__init__.py:{lineno} imports {module}")

        assert violations == [], (
            "sdk/__init__.py→bricks.rebac top-level import violations found (#3230):\n"
            + "\n".join(f"  - {v}" for v in violations)
        )


class TestZoneHelpersInLib:
    """Verify zone helpers are importable from lib/ (Issue #1519, 3A)."""

    def test_zone_helpers_importable_from_lib(self):
        from nexus.lib.zone_helpers import zone_group_id

        assert zone_group_id("acme") == "zone-acme"

    def test_zone_helpers_callable(self):
        from nexus.lib.zone_helpers import is_zone_admin

        assert callable(is_zone_admin)
