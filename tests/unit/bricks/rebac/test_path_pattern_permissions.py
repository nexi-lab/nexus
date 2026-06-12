"""ReBAC path-pattern tuple behavior for issue #4239."""

from datetime import UTC, datetime

import pytest

pytest.importorskip("pyroaring")

from sqlalchemy import create_engine

from nexus.bricks.rebac.consistency.metastore_namespace_store import MetastoreNamespaceStore
from nexus.bricks.rebac.domain import NamespaceConfig
from nexus.bricks.rebac.manager import ReBACManager
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


def test_recursive_descendant_grant(rebac_manager):
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="read",
        object=("file", "/workspaces/**"),
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/ws1/a.md"),
        )
        is True
    )


def test_recursive_prefix_path_grant(rebac_manager):
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="read",
        object=("file", "/workspaces/**"),
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces"),
        )
        is True
    )


def test_single_level_child_but_not_grandchild(rebac_manager):
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="read",
        object=("file", "/workspaces/*"),
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/a.md"),
        )
        is True
    )
    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/ws1/a.md"),
        )
        is False
    )


def test_no_partial_prefix_match(rebac_manager):
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="read",
        object=("file", "/workspaces/**"),
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces2/a.md"),
        )
        is False
    )


def test_non_file_pattern_looking_object_stays_exact_only(rebac_manager):
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member",
        object=("group", "/teams/**"),
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("group", "/teams/**"),
        )
        is True
    )
    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("group", "/teams/dev"),
        )
        is False
    )


def test_wildcard_subject_pattern_grants(rebac_manager):
    rebac_manager.rebac_write(
        subject=("*", "*"),
        relation="read",
        object=("file", "/public/**"),
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/public/docs/readme.md"),
        )
        is True
    )


def test_userset_as_subject_pattern_grants(rebac_manager):
    rebac_manager.rebac_write(
        subject=("agent", "alice"),
        relation="member",
        object=("group", "eng"),
    )
    rebac_manager.rebac_write(
        subject=("group", "eng", "member"),
        relation="read",
        object=("file", "/workspaces/**"),
    )

    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/ws1/a.md"),
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
            context={"current_time": inside_window, "time": inside_window},
        )
        is True
    )
    assert (
        rebac_manager.rebac_check(
            subject=("agent", "alice"),
            permission="read",
            object=("file", "/workspaces/ws1/a.md"),
            context={"current_time": outside_window, "time": outside_window},
        )
        is False
    )
