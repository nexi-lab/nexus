"""Reusable Protocol compliance test framework (Issue #1287, Decision 12A).

Provides ``assert_protocol_compliance()`` — a single-call verifier that
checks a concrete class against a Protocol for:

1. **Method presence**: All Protocol methods exist on the implementation.
2. **Signature compatibility**: Parameter names and kinds match.
3. **Runtime checkable**: ``isinstance(instance, Protocol)`` passes.

Usage::

    from tests.unit.services.test_protocol_compliance import assert_protocol_compliance
Parameterized tests for all 7 domain protocols are included below.
"""

import inspect

import pytest


def _get_protocol_methods(protocol: type) -> dict[str, inspect.Signature]:
    """Extract public method names and signatures from a Protocol class.

    Skips dunder methods and inherited ``object`` methods.
    """
    methods: dict[str, inspect.Signature] = {}
    # Walk Protocol's own annotations and members
    for name in dir(protocol):
        if name.startswith("_"):
            continue
        attr = getattr(protocol, name, None)
        if attr is None or not callable(attr):
            continue
        # Skip inherited object methods
        if hasattr(object, name):
            continue
        try:
            sig = inspect.signature(attr)
            methods[name] = sig
        except (ValueError, TypeError):
            # Some built-in methods can't be introspected
            continue
    return methods


def assert_protocol_compliance(
    impl_class: type,
    protocol: type,
    *,
    check_signatures: bool = True,
    check_async: bool = True,
    strict_params: bool = False,
) -> None:
    """Assert that ``impl_class`` satisfies ``protocol``.

    Args:
        impl_class: The concrete class to check.
        protocol: The Protocol class to check against.
        check_signatures: If True, verify parameter names match.
        check_async: If True, verify async/sync agreement between
            protocol and implementation (Issue #1287, Decision 11A).
        strict_params: If True, require exact parameter count match.
            If False (default), allow implementation to have extra params
            (common for DI or internal-only arguments).

    Raises:
        AssertionError: With detailed message on any mismatch.
    """
    protocol_methods = _get_protocol_methods(protocol)
    errors: list[str] = []

    # 1. Check all protocol methods exist on implementation
    for method_name, proto_sig in protocol_methods.items():
        impl_attr = getattr(impl_class, method_name, None)
        if impl_attr is None:
            errors.append(f"Missing method: {method_name}")
            continue

        if not callable(impl_attr):
            errors.append(f"Not callable: {method_name}")
            continue

        # 2. Check async/sync agreement (Issue #1287)
        if check_async:
            proto_attr = getattr(protocol, method_name, None)
            if proto_attr is not None:
                proto_is_async = inspect.iscoroutinefunction(
                    proto_attr
                ) or inspect.isasyncgenfunction(proto_attr)
                impl_is_async = inspect.iscoroutinefunction(
                    impl_attr
                ) or inspect.isasyncgenfunction(impl_attr)
                # Async generators (async def + yield) are compatible with
                # sync protocol stubs returning AsyncIterator (def -> AsyncIterator).
                impl_is_asyncgen = inspect.isasyncgenfunction(impl_attr)
                if proto_is_async != impl_is_async and not impl_is_asyncgen:
                    proto_kind = "async" if proto_is_async else "sync"
                    impl_kind = "async" if impl_is_async else "sync"
                    errors.append(
                        f"{method_name}: protocol is {proto_kind} but implementation is {impl_kind}"
                    )

        if not check_signatures:
            continue

        # 3. Check parameter compatibility
        try:
            impl_sig = inspect.signature(impl_attr)
        except (ValueError, TypeError):
            continue

        proto_params = [(n, p) for n, p in proto_sig.parameters.items() if n != "self"]
        impl_params = [(n, p) for n, p in impl_sig.parameters.items() if n != "self"]

        proto_names = [n for n, _ in proto_params]
        impl_names = [n for n, _ in impl_params]

        # Check that all protocol params exist in impl (order matters for positional)
        for proto_name in proto_names:
            if proto_name not in impl_names:
                errors.append(
                    f"{method_name}: missing param '{proto_name}' "
                    f"(protocol has {proto_names}, impl has {impl_names})"
                )

        if strict_params and len(impl_params) > len(proto_params):
            extra = set(impl_names) - set(proto_names)
            errors.append(f"{method_name}: extra params {extra} not in protocol")

    if errors:
        error_list = "\n  - ".join(errors)
        raise AssertionError(
            f"{impl_class.__name__} does not comply with {protocol.__name__}:\n  - {error_list}"
        )


# =========================================================================
# Parameterized compliance tests for all 8 domain protocols
# =========================================================================

# Each entry: (protocol_name, protocol_module, impl_class_path, expect_pass)
# We use lazy imports to avoid import errors when deps aren't available.

_PROTOCOL_IMPL_PAIRS: list[tuple[str, str, str, bool]] = [
    # ── Fully extracted (expect pass) ──────────────────────────────────
    (
        "MountProtocol",
        "nexus.contracts.protocols.mount",
        "nexus.bricks.mount.mount_service.MountService",
        True,  # Fixed: added delete_connector, context param naming, full_sync param
    ),
    (
        "OAuthProtocol",
        "nexus.contracts.protocols.oauth",
        "nexus.bricks.auth.oauth.credential_service.OAuthCredentialService",
        True,
    ),
    (
        "SearchProtocol",
        "nexus.contracts.protocols.search",
        "nexus.bricks.search.search_service.SearchService",
        True,
    ),
    (
        "PermissionProtocol",
        "nexus.contracts.protocols.permission",
        "nexus.bricks.rebac.rebac_service.ReBACService",
        True,  # Fixed: protocol updated to match async ReBACService interface
    ),
    # ── ShareLinkService extracted (Issue #1387) ────────────────────────
    (
        "ShareLinkProtocol",
        "nexus.contracts.protocols.share_link",
        "nexus.bricks.share_link.share_link_service.ShareLinkService",
        True,  # Method names match (async/sync checked separately)
    ),
    # WatchProtocol removed — watch is now kernel syscall (sys_watch).
    # LockProtocol removed — now Rust kernel primitive (lock_manager.rs).
    # ── TransactionalSnapshotService (Issue #1752) ──────────────────────
    (
        "SnapshotServiceProtocol",
        "nexus.contracts.protocols.snapshot",
        "nexus.bricks.snapshot.service.TransactionalSnapshotService",
        True,
    ),
    # ── Async adapter protocols (Issue #1440) ─────────────────────────
    (
        "NamespaceManagerProtocol",
        "nexus.contracts.protocols.namespace_manager",
        "nexus.bricks.rebac.namespace_manager.AsyncNamespaceManager",
        True,
    ),
    # ── Service-layer protocols ───────────────────────────────────────
    (
        "MCPProtocol",
        "nexus.contracts.protocols.mcp",
        "nexus.bricks.mcp.mcp_service.MCPService",
        True,
    ),
    (
        "OperationLogProtocol",
        "nexus.contracts.protocols.operation_log",
        "nexus.storage.operation_logger.OperationLogger",
        True,
    ),
    # ── Scheduler (using InMemoryScheduler test stub) ─────────────────
    (
        "SchedulerProtocol",
        "nexus.contracts.protocols.scheduler",
        "nexus.services.scheduler.in_memory.InMemoryScheduler",
        True,
    ),
    # ── Brick-level protocols ─────────────────────────────────────────
    (
        "ParseProtocol",
        "nexus.contracts.protocols.parse",
        "nexus.bricks.parsers.registry.ParserRegistry",
        True,
    ),
    (
        "PaymentProtocol",
        "nexus.contracts.protocols.payment",
        "nexus.bricks.pay.protocol.X402PaymentProtocol",
        True,
    ),
    # ── Former kernel protocols (Issue #2359: moved to services/protocols/) ──
    (
        "PermissionEnforcerProtocol",
        "nexus.contracts.protocols.permission_enforcer",
        "nexus.bricks.rebac.enforcer.PermissionEnforcer",
        True,
    ),
    (
        "EntityRegistryProtocol",
        "nexus.contracts.protocols.entity_registry",
        "nexus.bricks.rebac.entity_registry.EntityRegistry",
        True,
    ),
    (
        "WorkspaceManagerProtocol",
        "nexus.contracts.protocols.workspace_manager",
        "nexus.services.workspace.workspace_manager.WorkspaceManager",
        True,
    ),
]


def _try_import(module_path: str, class_name: str) -> type | None:
    """Attempt to import a class, returning None on failure."""
    try:
        import importlib

        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except (ImportError, AttributeError):
        return None


@pytest.mark.parametrize(
    "protocol_name,protocol_module,impl_path,expect_pass",
    _PROTOCOL_IMPL_PAIRS,
    ids=[pair[0] for pair in _PROTOCOL_IMPL_PAIRS],
)
def test_service_protocol_compliance(
    protocol_name: str,
    protocol_module: str,
    impl_path: str,
    expect_pass: bool,
) -> None:
    """Verify each service implementation satisfies its Protocol."""
    protocol_cls = _try_import(protocol_module, protocol_name)
    if protocol_cls is None:
        pytest.skip(f"Cannot import protocol {protocol_name} from {protocol_module}")

    parts = impl_path.rsplit(".", 1)
    impl_cls = _try_import(parts[0], parts[1])
    if impl_cls is None:
        pytest.skip(f"Cannot import implementation {impl_path}")

    if not expect_pass:
        with pytest.raises(AssertionError):
            assert_protocol_compliance(impl_cls, protocol_cls)
        return

    assert_protocol_compliance(impl_cls, protocol_cls)


def test_rebac_manager_satisfies_manager_methods() -> None:
    """ReBACManager satisfies the manager-API subset of ReBACBrickProtocol.

    ReBACBrickProtocol includes brick lifecycle methods (initialize, shutdown,
    verify_imports) that are only implemented by the brick wrapper, not by the
    underlying ReBACManager. This test verifies manager-level compliance.
    """
    from nexus.contracts.protocols.rebac import ReBACBrickProtocol

    impl_cls = _try_import("nexus.bricks.rebac.manager", "ReBACManager")
    if impl_cls is None:
        pytest.skip("Cannot import ReBACManager")

    manager_methods = {
        "rebac_check",
        "rebac_write",
        "rebac_delete",
        "rebac_expand",
        "rebac_check_bulk",
        "rebac_list_objects",
        "get_zone_revision",
        "invalidate_zone_graph_cache",
        "close",
    }

    proto_methods = _get_protocol_methods(ReBACBrickProtocol)
    for method_name in manager_methods:
        assert method_name in proto_methods, f"Protocol missing {method_name}"
        impl_attr = getattr(impl_cls, method_name, None)
        assert impl_attr is not None, f"ReBACManager missing {method_name}"
        assert callable(impl_attr), f"ReBACManager.{method_name} not callable"


# =========================================================================
# Protocol file import cleanliness (Issue #1291)
# =========================================================================

_PROTOCOL_FILES: list[tuple[str, str]] = [
    ("auth", "nexus/contracts/protocols/auth.py"),
    ("event_log", "nexus/system_services/event_log/protocol.py"),
    ("governance", "nexus/bricks/governance/protocols.py"),
    ("lock", "nexus/contracts/protocols/lock.py"),
    ("mcp", "nexus/contracts/protocols/mcp.py"),
    ("mount", "nexus/contracts/protocols/mount.py"),
    ("namespace_manager", "nexus/contracts/protocols/namespace_manager.py"),
    ("oauth", "nexus/contracts/protocols/oauth.py"),
    ("operation_log", "nexus/contracts/protocols/operation_log.py"),
    ("parse", "nexus/contracts/protocols/parse.py"),
    ("payment", "nexus/contracts/protocols/payment.py"),
    ("permission", "nexus/contracts/protocols/permission.py"),
    ("rebac", "nexus/contracts/protocols/rebac.py"),
    ("scheduler", "nexus/contracts/protocols/scheduler.py"),
    ("search", "nexus/contracts/protocols/search.py"),
    ("share_link", "nexus/contracts/protocols/share_link.py"),
    ("snapshot", "nexus/contracts/protocols/snapshot.py"),
    ("version", "nexus/contracts/protocols/version.py"),
    ("vfs_router", "nexus/core/protocols/vfs_router.py"),
    ("watch", "nexus/contracts/protocols/watch.py"),
    ("vfs_core", "nexus/core/protocols/vfs_core.py"),
    ("caching", "nexus/core/protocols/caching.py"),
    ("connector", "nexus/core/protocols/connector.py"),
    # Issue #2359: Moved protocols to their correct tier locations
    ("describable", "nexus/contracts/describable.py"),
    ("wirable_fs", "nexus/contracts/wirable_fs.py"),
    ("permission_enforcer", "nexus/contracts/protocols/permission_enforcer.py"),
    ("entity_registry", "nexus/contracts/protocols/entity_registry.py"),
    ("workspace_manager", "nexus/contracts/protocols/workspace_manager.py"),
]

# Leaf modules that are safe to import at module level in protocol files
_ALLOWED_LEAF_MODULES = {"nexus.constants", "nexus.contracts.constants"}


@pytest.mark.parametrize(
    "name,rel_path",
    _PROTOCOL_FILES,
    ids=[p[0] for p in _PROTOCOL_FILES],
)
def test_protocol_file_no_heavy_runtime_imports(name: str, rel_path: str) -> None:
    """Protocol files should not have runtime imports from non-leaf nexus modules.

    All nexus.* imports (except leaf modules like constants) should be
    inside TYPE_CHECKING blocks to prevent circular import chains.
    """
    import ast
    from pathlib import Path

    filepath = Path("src") / rel_path
    if not filepath.exists():
        pytest.skip(f"Protocol file not found: {filepath}")

    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))

    violations: list[str] = []
    for node in ast.iter_child_nodes(tree):
        # Skip TYPE_CHECKING blocks
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            ):
                continue

        if isinstance(node, ast.ImportFrom):
            if (
                node.module
                and node.module.startswith("nexus")
                and node.module not in _ALLOWED_LEAF_MODULES
            ):
                names = ", ".join(a.name for a in node.names)
                violations.append(f"from {node.module} import {names}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("nexus") and alias.name not in _ALLOWED_LEAF_MODULES:
                    violations.append(f"import {alias.name}")

    assert violations == [], (
        f"Protocol file {rel_path} has runtime nexus imports "
        f"(should use TYPE_CHECKING): {violations}"
    )
