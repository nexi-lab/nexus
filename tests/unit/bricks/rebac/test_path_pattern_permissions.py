"""ReBAC path-pattern tuple behavior for issue #4239."""

from datetime import UTC, datetime

import pytest

pytest.importorskip("pyroaring")

from sqlalchemy import create_engine

from nexus.bricks.rebac.consistency.metastore_namespace_store import MetastoreNamespaceStore
from nexus.bricks.rebac.default_namespaces import DEFAULT_FILE_NAMESPACE
from nexus.bricks.rebac.domain import NamespaceConfig
from nexus.bricks.rebac.enforcer import PermissionEnforcer
from nexus.bricks.rebac.manager import ReBACManager
from nexus.contracts.types import OperationContext
from nexus.storage.models import Base
from tests.testkit.metadata import InMemoryNexusFS


@pytest.fixture
def rebac_manager():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    manager = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )
    manager.create_namespace(
        NamespaceConfig(
            namespace_id="path-pattern-permissions-file",
            object_type="file",
            config={"relations": {"read": {}}, "permissions": {}},
        )
    )
    try:
        yield manager
    finally:
        manager.close()


@pytest.fixture
def zone_aware_rebac_manager():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    manager = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
        enforce_zone_isolation=True,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )
    manager.create_namespace(
        NamespaceConfig(
            namespace_id="path-pattern-permissions-file-zone-aware",
            object_type="file",
            config={"relations": {"read": {}}, "permissions": {}},
        )
    )
    try:
        yield manager
    finally:
        manager.close()


@pytest.fixture
def default_file_rebac_manager():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    manager = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )
    manager.create_namespace(DEFAULT_FILE_NAMESPACE)
    try:
        yield manager
    finally:
        manager.close()


@pytest.fixture
def zone_aware_default_file_rebac_manager():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    manager = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
        enforce_zone_isolation=True,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )
    manager.create_namespace(DEFAULT_FILE_NAMESPACE)
    try:
        yield manager
    finally:
        manager.close()


def test_recursive_descendant_grant(rebac_manager):
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/ws1/a.md"),
            zone_id="root",
        )
        is True
    )


def test_recursive_prefix_path_grant(rebac_manager):
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces"),
            zone_id="root",
        )
        is True
    )


def test_rebac_expand_includes_recursive_path_pattern_subject(rebac_manager) -> None:
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    assert ("agent", "alice") in rebac_manager.rebac_expand(
        permission="read",
        object=("file", "/workspaces/ws1/a.md"),
        zone_id="root",
    )


def test_single_level_child_but_not_grandchild(rebac_manager):
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="read",
        object=("file", "/workspaces/*"),
        zone_id="root",
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/a.md"),
            zone_id="root",
        )
        is True
    )
    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/ws1/a.md"),
            zone_id="root",
        )
        is False
    )


def test_default_file_single_level_direct_viewer_does_not_inherit_to_grandchild(
    default_file_rebac_manager,
) -> None:
    default_file_rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="direct_viewer",
        object=("file", "/workspaces/*"),
        zone_id="root",
    )

    assert (
        default_file_rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/a.md"),
            zone_id="root",
        )
        is True
    )
    assert (
        default_file_rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/ws1/a.md"),
            zone_id="root",
        )
        is False
    )
    assert ("agent", "alice") not in default_file_rebac_manager.rebac_expand(
        permission="read",
        object=("file", "/workspaces/ws1/a.md"),
        zone_id="root",
    )


def test_zone_aware_recursive_path_pattern_grants_descendant_read(
    zone_aware_rebac_manager,
):
    zone_aware_rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    assert (
        zone_aware_rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/ws1/a.md"),
            zone_id="root",
        )
        is True
    )


def test_zone_aware_rebac_expand_includes_recursive_path_pattern_subject(
    zone_aware_rebac_manager,
) -> None:
    zone_aware_rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    assert ("agent", "alice") in zone_aware_rebac_manager.rebac_expand(
        permission="read",
        object=("file", "/workspaces/ws1/a.md"),
        zone_id="root",
    )


def test_zone_aware_default_file_single_level_direct_viewer_does_not_inherit_to_grandchild(
    zone_aware_default_file_rebac_manager,
) -> None:
    zone_aware_default_file_rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="direct_viewer",
        object=("file", "/workspaces/*"),
        zone_id="root",
    )

    assert (
        zone_aware_default_file_rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/a.md"),
            zone_id="root",
        )
        is True
    )
    assert (
        zone_aware_default_file_rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/ws1/a.md"),
            zone_id="root",
        )
        is False
    )
    assert ("agent", "alice") not in zone_aware_default_file_rebac_manager.rebac_expand(
        permission="read",
        object=("file", "/workspaces/ws1/a.md"),
        zone_id="root",
    )


def test_zone_aware_wildcard_path_pattern_grants_cross_zone_public_read(
    zone_aware_rebac_manager,
):
    zone_aware_rebac_manager.rebac_write(
        subject=("*", "*"),
        relation="read",
        object=("file", "/public/**"),
        zone_id="shared",
    )

    assert (
        zone_aware_rebac_manager.rebac_check(
            subject=("agent", "visitor"),
            permission="read",
            object=("file", "/public/docs/readme.md"),
            zone_id="root",
        )
        is True
    )


def test_zone_aware_userset_subject_path_pattern_grants_member_read(
    zone_aware_rebac_manager,
):
    zone_aware_rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member",
        object=("group", "eng"),
        zone_id="root",
    )
    zone_aware_rebac_manager.rebac_write(
        subject=("group", "eng", "member"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    assert (
        zone_aware_rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/ws1/a.md"),
            zone_id="root",
        )
        is True
    )


def test_group_tuple_to_userset_path_pattern_grants_member_read() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    manager = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )
    try:
        manager.rebac_write(
            subject=("agent", "alice"),
            relation="member",
            object=("group", "eng"),
            zone_id="root",
        )
        manager.rebac_write(
            subject=("group", "eng"),
            relation="direct_viewer",
            object=("file", "/workspaces/**"),
            zone_id="root",
        )

        target = ("file", "/workspaces/ws1/a.md")
        check = (("agent", "alice"), "read", target)

        assert (
            manager.rebac_check(
                subject=("agent", "alice"),
                permission="read",
                object=target,
                zone_id="root",
            )
            is True
        )
        assert manager.rebac_check_bulk([check], zone_id="root") == {check: True}
    finally:
        manager.close()


def test_single_check_skips_tiger_write_through_for_path_pattern_grant(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    manager = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )
    try:
        manager.rebac_write(
            subject=("agent", "alice"),
            relation="direct_viewer",
            object=("file", "/workspaces/**"),
            zone_id="root",
        )

        tiger_writes: list[tuple[tuple[str, str], str, tuple[str, str]]] = []
        monkeypatch.setattr(manager, "_tiger_cache", object())
        monkeypatch.setattr(manager, "tiger_check_access", lambda **_kwargs: None)
        monkeypatch.setattr(
            manager._tuple_writer,
            "tiger_write_through_single",
            lambda *, subject, permission, object, **_kwargs: tiger_writes.append(
                (subject, permission, object)
            ),
        )

        assert (
            manager.rebac_check(
                subject=("agent", "alice"),
                permission="read",
                object=("file", "/workspaces/ws1/a.md"),
                zone_id="root",
            )
            is True
        )
        assert tiger_writes == []
    finally:
        manager.close()


def test_path_pattern_write_invalidates_tiger_instead_of_persisting_pattern(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    manager = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
        enable_tiger_cache=False,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )
    try:
        grants: list[tuple[tuple[str, str], str, str, str, str]] = []
        invalidations: list[tuple[tuple[str, str], str, str, str]] = []
        monkeypatch.setattr(manager, "_tiger_cache", object())
        monkeypatch.setattr(
            manager,
            "tiger_persist_grant",
            lambda *, subject, permission, resource_type, resource_id, zone_id: grants.append(
                (subject, permission, resource_type, resource_id, zone_id)
            ),
        )
        monkeypatch.setattr(
            manager,
            "tiger_invalidate_cache",
            lambda subject, permission, resource_type, zone_id: invalidations.append(
                (subject, permission, resource_type, zone_id)
            ),
        )

        manager.rebac_write(
            subject=("agent", "alice"),
            relation="direct_viewer",
            object=("file", "/workspaces/**"),
            zone_id="root",
        )

        assert grants == []
        assert invalidations == [(("agent", "alice"), "read", "file", "root")]
    finally:
        manager.close()


def test_path_pattern_delete_invalidates_tiger_instead_of_exact_revoke(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    manager = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
        enable_tiger_cache=False,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )
    try:
        result = manager.rebac_write(
            subject=("agent", "alice"),
            relation="direct_viewer",
            object=("file", "/workspaces/**"),
            zone_id="root",
        )

        revokes: list[tuple[tuple[str, str], str, str, str, str]] = []
        invalidations: list[tuple[tuple[str, str], str, str, str]] = []
        monkeypatch.setattr(manager, "_tiger_cache", object())
        monkeypatch.setattr(
            manager,
            "tiger_persist_revoke",
            lambda *, subject, permission, resource_type, resource_id, zone_id: revokes.append(
                (subject, permission, resource_type, resource_id, zone_id)
            ),
        )
        monkeypatch.setattr(
            manager,
            "tiger_invalidate_cache",
            lambda subject, permission, resource_type, zone_id: invalidations.append(
                (subject, permission, resource_type, zone_id)
            ),
        )

        assert manager.rebac_delete(result)
        assert revokes == []
        assert invalidations == [(("agent", "alice"), "read", "file", "root")]
    finally:
        manager.close()


def test_no_partial_prefix_match(rebac_manager):
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces2/a.md"),
            zone_id="root",
        )
        is False
    )


def test_non_file_pattern_looking_object_stays_exact_only(rebac_manager):
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member",
        object=("group", "/teams/**"),
        zone_id="root",
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="member",
            object=("group", "/teams/**"),
            zone_id="root",
        )
        is True
    )
    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="member",
            object=("group", "/teams/dev"),
            zone_id="root",
        )
        is False
    )


def test_wildcard_subject_pattern_grants(rebac_manager):
    rebac_manager.rebac_write(
        subject=("*", "*"),
        relation="read",
        object=("file", "/public/**"),
        zone_id="root",
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/public/docs/readme.md"),
            zone_id="root",
        )
        is True
    )


def test_userset_as_subject_pattern_grants(rebac_manager):
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member",
        object=("group", "eng"),
        zone_id="root",
    )
    rebac_manager.rebac_write(
        subject=("group", "eng", "member"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/ws1/a.md"),
            zone_id="root",
        )
        is True
    )


def test_abac_conditions_are_preserved_for_pattern_tuples(rebac_manager):
    inside_window = datetime(2026, 1, 1, 12, tzinfo=UTC).isoformat()
    outside_window = datetime(2026, 1, 3, 20, tzinfo=UTC).isoformat()

    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
        conditions={
            "time_window": {
                "start": datetime(2026, 1, 1, 9, tzinfo=UTC).isoformat(),
                "end": datetime(2026, 1, 2, 17, tzinfo=UTC).isoformat(),
            }
        },
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/ws1/a.md"),
            zone_id="root",
            context={"current_time": inside_window, "time": inside_window},
        )
        is True
    )
    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/ws1/a.md"),
            zone_id="root",
            context={"current_time": outside_window, "time": outside_window},
        )
        is False
    )


def test_rebac_check_bulk_matches_recursive_path_pattern(rebac_manager) -> None:
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    checks = [
        (("agent", "alice"), "read", ("file", "/workspaces/ws1/a.md")),
        (("agent", "alice"), "read", ("file", "/private/a.md")),
    ]

    assert rebac_manager.rebac_check_bulk(checks, zone_id="root") == {
        checks[0]: True,
        checks[1]: False,
    }


def test_rebac_check_bulk_matches_userset_path_pattern(rebac_manager) -> None:
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member",
        object=("group", "eng"),
        zone_id="root",
    )
    rebac_manager.rebac_write(
        subject=("group", "eng", "member"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    checks = [
        (("agent", "alice"), "read", ("file", "/workspaces/ws1/a.md")),
        (("agent", "bob"), "read", ("file", "/workspaces/ws1/a.md")),
    ]

    assert rebac_manager.rebac_check_bulk(checks, zone_id="root") == {
        checks[0]: True,
        checks[1]: False,
    }


def test_rebac_check_bulk_denies_conditioned_path_pattern_without_context(
    rebac_manager,
) -> None:
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
        conditions={"department": "eng"},
    )

    check = (("agent", "alice"), "read", ("file", "/workspaces/ws1/a.md"))

    assert rebac_manager.rebac_check_bulk([check], zone_id="root") == {check: False}


def test_rebac_check_bulk_keeps_non_file_pattern_syntax_exact_only(
    rebac_manager,
) -> None:
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member",
        object=("group", "/teams/**"),
        zone_id="root",
    )

    checks = [
        (("agent", "alice"), "member", ("group", "/teams/**")),
        (("agent", "alice"), "member", ("group", "/teams/dev")),
    ]

    assert rebac_manager.rebac_check_bulk(checks, zone_id="root") == {
        checks[0]: True,
        checks[1]: False,
    }


def test_filter_list_matches_recursive_path_pattern(rebac_manager) -> None:
    rebac_manager.rebac_write(
        subject=("user", "admin"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )
    enforcer = PermissionEnforcer(rebac_manager=rebac_manager)
    context = OperationContext(
        user_id="admin",
        groups=[],
        subject_type="user",
        subject_id="admin",
        zone_id="root",
    )

    assert enforcer.filter_list(
        ["/workspaces/ws1/a.md", "/workspaces/ws2/b.md", "/private/c.md"],
        context,
    ) == ["/workspaces/ws1/a.md", "/workspaces/ws2/b.md"]


def test_filter_list_matches_userset_path_pattern(rebac_manager) -> None:
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member",
        object=("group", "eng"),
        zone_id="root",
    )
    rebac_manager.rebac_write(
        subject=("group", "eng", "member"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )
    enforcer = PermissionEnforcer(rebac_manager=rebac_manager)
    context = OperationContext(
        user_id="alice",
        groups=[],
        subject_type="agent",
        subject_id="alice",
        zone_id="root",
    )

    assert enforcer.filter_list(
        ["/workspaces/ws1/a.md", "/private/c.md"],
        context,
    ) == ["/workspaces/ws1/a.md"]


def test_zone_aware_bulk_matches_cross_zone_wildcard_path_pattern(
    zone_aware_rebac_manager,
) -> None:
    zone_aware_rebac_manager.rebac_write(
        subject=("*", "*"),
        relation="read",
        object=("file", "/public/**"),
        zone_id="shared",
    )

    checks = [
        (("user", "visitor"), "read", ("file", "/public/docs/readme.md")),
        (("user", "visitor"), "read", ("file", "/private/readme.md")),
    ]

    assert zone_aware_rebac_manager.rebac_check_bulk(checks, zone_id="root") == {
        checks[0]: True,
        checks[1]: False,
    }


def test_zone_aware_filter_list_matches_cross_zone_wildcard_path_pattern(
    zone_aware_rebac_manager,
) -> None:
    zone_aware_rebac_manager.rebac_write(
        subject=("*", "*"),
        relation="read",
        object=("file", "/public/**"),
        zone_id="shared",
    )
    enforcer = PermissionEnforcer(rebac_manager=zone_aware_rebac_manager)
    context = OperationContext(
        user_id="visitor",
        groups=[],
        subject_type="user",
        subject_id="visitor",
        zone_id="root",
    )

    assert enforcer.filter_list(
        ["/public/docs/readme.md", "/public/guide.md", "/private/c.md"],
        context,
    ) == ["/public/docs/readme.md", "/public/guide.md"]


def test_python_fast_fallback_matches_direct_path_pattern_tuple() -> None:
    from nexus.bricks.rebac.utils.fast import check_permissions_bulk_with_fallback

    checks = [(("user", "admin"), "read", ("file", "/workspaces/a.md"))]
    tuples = [
        {
            "subject_type": "user",
            "subject_id": "admin",
            "subject_relation": None,
            "relation": "read",
            "object_type": "file",
            "object_id": "/workspaces/**",
        }
    ]

    assert (
        check_permissions_bulk_with_fallback(
            checks,
            tuples,
            {},
            force_python=True,
        )[("user", "admin", "read", "file", "/workspaces/a.md")]
        is True
    )


def test_fast_fallback_skips_rust_when_path_pattern_tuple_present(monkeypatch) -> None:
    from nexus.bricks.rebac.utils import fast

    checks = [(("user", "admin"), "read", ("file", "/workspaces/a.md"))]
    tuples = [
        {
            "subject_type": "user",
            "subject_id": "admin",
            "subject_relation": None,
            "relation": "read",
            "object_type": "file",
            "object_id": "/workspaces/**",
        }
    ]

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Rust bulk path should not run for path-pattern tuples")

    monkeypatch.setattr(fast, "RUST_AVAILABLE", True)
    monkeypatch.setattr(fast, "check_permissions_bulk_rust", fail_if_called)

    assert (
        fast.check_permissions_bulk_with_fallback(
            checks,
            tuples,
            {},
            force_python=False,
        )[("user", "admin", "read", "file", "/workspaces/a.md")]
        is True
    )


def test_path_pattern_write_invalidates_cached_descendant_denial(rebac_manager) -> None:
    target = ("file", "/workspaces/ws1/a.md")

    assert not rebac_manager.rebac_check(
        subject=("user", "admin"),
        permission="read",
        object=target,
        zone_id="root",
    )

    rebac_manager.rebac_write(
        subject=("user", "admin"),
        relation="read",
        object=("file", "/workspaces/**"),
        zone_id="root",
    )

    assert rebac_manager.rebac_check(
        subject=("user", "admin"),
        permission="read",
        object=target,
        zone_id="root",
    )


def test_path_pattern_tuple_change_invalidates_descendant_cache_entries() -> None:
    from nexus.bricks.rebac.cache.boundary import PermissionBoundaryCache
    from nexus.bricks.rebac.cache.coordinator import CacheCoordinator
    from nexus.bricks.rebac.cache.result_cache import ReBACPermissionCache
    from nexus.bricks.rebac.domain import Entity

    class FakeConnection:
        def commit(self) -> None:
            pass

    l1_cache = ReBACPermissionCache(
        max_size=100,
        ttl_seconds=300,
        denial_ttl_seconds=300,
        enable_revision_quantization=False,
    )
    boundary_cache = PermissionBoundaryCache()
    coordinator = CacheCoordinator(
        l1_cache=l1_cache,
        boundary_cache=boundary_cache,
        enable_async_recompute=False,
    )

    l1_cache.set(
        "user",
        "admin",
        "read",
        "file",
        "/workspaces/ws1/a.md",
        False,
        "root",
    )
    boundary_cache.set_boundary(
        "root",
        "user",
        "admin",
        "read",
        "/workspaces/ws1/a.md",
        "/workspaces",
    )

    assert (
        l1_cache.get(
            "user",
            "admin",
            "read",
            "file",
            "/workspaces/ws1/a.md",
            "root",
        )
        is False
    )
    assert (
        boundary_cache.get_boundary(
            "root",
            "user",
            "admin",
            "read",
            "/workspaces/ws1/a.md",
        )
        == "/workspaces"
    )

    coordinator.invalidate_for_tuple_change(
        Entity("user", "admin"),
        "read",
        Entity("file", "/workspaces/**"),
        zone_id="root",
        conn=FakeConnection(),
    )

    assert (
        l1_cache.get(
            "user",
            "admin",
            "read",
            "file",
            "/workspaces/ws1/a.md",
            "root",
        )
        is None
    )


def test_root_path_pattern_tuple_change_invalidates_boundary_descendants() -> None:
    from nexus.bricks.rebac.cache.boundary import PermissionBoundaryCache
    from nexus.bricks.rebac.cache.coordinator import CacheCoordinator
    from nexus.bricks.rebac.domain import Entity

    class FakeConnection:
        def commit(self) -> None:
            pass

    boundary_cache = PermissionBoundaryCache()
    coordinator = CacheCoordinator(
        boundary_cache=boundary_cache,
        enable_async_recompute=False,
    )
    boundary_cache.set_boundary(
        "root",
        "user",
        "admin",
        "read",
        "/a/b.md",
        "/a",
    )

    assert (
        boundary_cache.get_boundary(
            "root",
            "user",
            "admin",
            "read",
            "/a/b.md",
        )
        == "/a"
    )

    coordinator.invalidate_for_tuple_change(
        Entity("user", "admin"),
        "read",
        Entity("file", "/**"),
        zone_id="root",
        conn=FakeConnection(),
    )

    assert (
        boundary_cache.get_boundary(
            "root",
            "user",
            "admin",
            "read",
            "/a/b.md",
        )
        is None
    )


def test_path_pattern_tuple_change_invalidates_registered_boundary_cache() -> None:
    from nexus.bricks.rebac.cache.boundary import PermissionBoundaryCache
    from nexus.bricks.rebac.cache.coordinator import CacheCoordinator
    from nexus.bricks.rebac.domain import Entity

    class FakeConnection:
        def commit(self) -> None:
            pass

    registered_boundary_cache = PermissionBoundaryCache()
    coordinator = CacheCoordinator(enable_async_recompute=False)
    coordinator.register_boundary_invalidator(
        "registered",
        registered_boundary_cache.invalidate_permission_change,
    )
    registered_boundary_cache.set_boundary(
        "root",
        "user",
        "admin",
        "read",
        "/workspaces/ws1/a.md",
        "/workspaces",
    )

    assert (
        registered_boundary_cache.get_boundary(
            "root",
            "user",
            "admin",
            "read",
            "/workspaces/ws1/a.md",
        )
        == "/workspaces"
    )

    coordinator.invalidate_for_tuple_change(
        Entity("user", "admin"),
        "read",
        Entity("file", "/workspaces/**"),
        zone_id="root",
        conn=FakeConnection(),
    )

    assert (
        registered_boundary_cache.get_boundary(
            "root",
            "user",
            "admin",
            "read",
            "/workspaces/ws1/a.md",
        )
        is None
    )
