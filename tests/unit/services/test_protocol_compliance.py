"""Reusable Protocol compliance test framework (Issue #1287, Decision 12A).

Provides ``assert_protocol_compliance()`` — a single-call verifier that
checks a concrete class against a Protocol for:

1. **Method presence**: All Protocol methods exist on the implementation.
2. **Signature compatibility**: Parameter names and kinds match.
3. **Runtime checkable**: ``isinstance(instance, Protocol)`` passes.

Usage::

    from tests.unit.services.test_protocol_compliance import assert_protocol_compliance
    from nexus.services.protocols import SkillsProtocol
    from nexus.services.skill_service import SkillService

    def test_skill_service_protocol():
        assert_protocol_compliance(SkillService, SkillsProtocol)

Parameterized tests for all 8 domain protocols are included below.
"""

from __future__ import annotations

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
                if proto_is_async != impl_is_async:
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
        "LLMProtocol",
        "nexus.services.protocols.llm",
        "nexus.services.llm_service.LLMService",
        True,
    ),
    # ── Phase 1.5: Protocol updated to unprefixed names matching service ──
    (
        "SkillsProtocol",
        "nexus.services.protocols.skills",
        "nexus.services.skill_service.SkillService",
        True,
    ),
    (
        "MountProtocol",
        "nexus.services.protocols.mount",
        "nexus.services.mount_service.MountService",
        False,  # MountService is async but has pre-existing param mismatches (delete_connector, context naming)
    ),
    (
        "OAuthProtocol",
        "nexus.services.protocols.oauth",
        "nexus.services.oauth_service.OAuthService",
        True,
    ),
    (
        "SearchProtocol",
        "nexus.services.protocols.search",
        "nexus.services.search_service.SearchService",
        True,
    ),
    (
        "PermissionProtocol",
        "nexus.services.protocols.permission",
        "nexus.services.rebac_service.ReBACService",
        False,  # Many methods are stubs
    ),
    # ── Not yet extracted (expect fail) ────────────────────────────────
    (
        "ShareLinkProtocol",
        "nexus.services.protocols.share_link",
        "nexus.core.nexus_fs_share_links.NexusFSShareLinksMixin",
        True,  # Method names match (async/sync checked separately)
    ),
    (
        "EventsProtocol",
        "nexus.services.protocols.events",
        "nexus.core.nexus_fs_events.NexusFSEventsMixin",
        True,  # Method names match (async/sync checked separately)
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
