"""Zone-control ReBAC binding semantics: a projected grant prefix covers
the prefix itself, not only its descendants.

The projection writes "{prefix}/*" tuples while grant prefix semantics
(services/zones/authz.py) cover the path itself; the binding's check()
must therefore include the prefix's own wildcard target — otherwise a
grant on /docs authorizes /docs/file but not /docs.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from nexus.bricks.rebac.consistency.metastore_namespace_store import MetastoreNamespaceStore
from nexus.bricks.rebac.default_namespaces import DEFAULT_FILE_NAMESPACE, DEFAULT_GROUP_NAMESPACE
from nexus.bricks.rebac.manager import ReBACManager
from nexus.contracts.rebac_types import GraphLimits
from nexus.server.lifespan.zone_control import _rebac_bindings
from nexus.storage.models import Base
from tests.testkit.metadata import InMemoryNexusFS

ZONE = "root"
PRINCIPAL = {"subject_type": "user", "subject_id": "bob"}
SUBJECT = "user:bob"
#: zone.data.read projects as relation direct_viewer and checks as read
RELATION = "direct_viewer"
PERMISSION = "read"


@pytest.fixture(autouse=True)
def _raise_graph_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # Slow hosts run the graph walk past the 1s DoS budget; the prefix
    # semantics under test are independent of the timeout knob.
    monkeypatch.setattr(GraphLimits, "MAX_EXECUTION_TIME_MS", 30_000)


@pytest.fixture
def bindings() -> tuple[Any, Any, Any]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        isolation_level="AUTOCOMMIT",
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS rebac_group_closure (
                    member_type VARCHAR(50) NOT NULL,
                    member_id VARCHAR(255) NOT NULL,
                    group_type VARCHAR(50) NOT NULL,
                    group_id VARCHAR(255) NOT NULL,
                    zone_id VARCHAR(255) NOT NULL,
                    depth INTEGER NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (member_type, member_id, group_type, group_id, zone_id)
                )
                """
            )
        )
    manager = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
        enforce_zone_isolation=True,
        enable_tiger_cache=False,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )
    manager.create_namespace(DEFAULT_FILE_NAMESPACE)
    manager.create_namespace(DEFAULT_GROUP_NAMESPACE)
    # The L1/boundary caches may answer before the zone-aware path; disable
    # them so these tests exercise the path the binding actually queries.
    manager._l1_cache = None
    manager._boundary_cache = None
    return _rebac_bindings(manager)


def test_grant_prefix_covers_the_prefix_itself(bindings: tuple[Any, Any, Any]) -> None:
    check, write, _delete = bindings
    write(ZONE, PRINCIPAL, RELATION, "/docs")
    assert check(None, SUBJECT, PERMISSION, "/docs", ZONE) is True
    assert check(None, SUBJECT, PERMISSION, "/docs/file", ZONE) is True


def test_unrelated_path_stays_denied(bindings: tuple[Any, Any, Any]) -> None:
    check, write, _delete = bindings
    write(ZONE, PRINCIPAL, RELATION, "/docs")
    assert check(None, SUBJECT, PERMISSION, "/other", ZONE) is False


def test_root_grant_covers_root_and_descendants(bindings: tuple[Any, Any, Any]) -> None:
    check, write, _delete = bindings
    write(ZONE, PRINCIPAL, RELATION, "/")
    assert check(None, SUBJECT, PERMISSION, "/", ZONE) is True
    assert check(None, SUBJECT, PERMISSION, "/docs", ZONE) is True
