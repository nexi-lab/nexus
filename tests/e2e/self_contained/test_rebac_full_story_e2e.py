"""End-to-end ReBAC story coverage for issue #4134.

These tests use a real EnhancedReBACManager backed by a shared in-memory
SQLite database.  CLI tests only replace the filesystem connection factory so
the Click command path can exercise the same real ReBAC service.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import nexus.cli.commands.rebac as rebac_cli
from nexus.bricks.rebac.consistency.metastore_namespace_store import MetastoreNamespaceStore
from nexus.bricks.rebac.manager import EnhancedReBACManager
from nexus.bricks.rebac.rebac_service import ReBACService
from nexus.storage.models import Base
from tests.testkit.metadata import InMemoryNexusFS

ZONE_ID = "story-zone"
RESOURCE = ("file", "/workspace/team/report.csv")


@pytest.fixture
def rebac_service() -> Iterator[ReBACService]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    manager = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=20,
        enforce_zone_isolation=True,
        enable_graph_limits=True,
        enable_leopard=True,
        enable_tiger_cache=False,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )
    try:
        yield ReBACService(
            rebac_manager=manager,
            enforce_permissions=False,
            enable_audit_logging=False,
        )
    finally:
        manager.close()


class _ServiceBackedFilesystem:
    def __init__(self, service: ReBACService):
        self._service = service
        self.closed = False

    def service(self, name: str) -> ReBACService | None:
        return self._service if name == "rebac" else None

    def close(self) -> None:
        self.closed = True


class _SyncRemoteReBACProxy:
    """RemoteServiceProxy-shaped service: RPC methods return sync values."""

    def rebac_list_objects(
        self,
        *,
        relation: str,  # noqa: ARG002
        subject: tuple[str, str],  # noqa: ARG002
        zone_id: str | None = None,  # noqa: ARG002
    ) -> list[list[str]]:
        return [["file", RESOURCE[1]]]


class _SyncRemoteFilesystem:
    def __init__(self) -> None:
        self._service = _SyncRemoteReBACProxy()
        self.closed = False

    def service(self, name: str) -> _SyncRemoteReBACProxy | None:
        return self._service if name == "rebac" else None

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def cli_runner(monkeypatch: pytest.MonkeyPatch, rebac_service: ReBACService) -> CliRunner:
    async def _get_filesystem(*_args: Any, **_kwargs: Any) -> _ServiceBackedFilesystem:
        return _ServiceBackedFilesystem(rebac_service)

    monkeypatch.setattr(rebac_cli, "get_filesystem", _get_filesystem)
    return CliRunner(env={"NEXUS_NO_AUTO_JSON": "1"})


@pytest.fixture
def remote_cli_runner(monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    async def _get_filesystem(*_args: Any, **_kwargs: Any) -> _SyncRemoteFilesystem:
        return _SyncRemoteFilesystem()

    monkeypatch.setattr(rebac_cli, "get_filesystem", _get_filesystem)
    return CliRunner(env={"NEXUS_NO_AUTO_JSON": "1"})


@pytest.mark.asyncio
async def test_rpc_share_list_revoke_round_trip_uses_shared_relations(
    rebac_service: ReBACService,
) -> None:
    user_share = await rebac_service.share_with_user(
        RESOURCE,
        "bob",
        permission="viewer",
        zone_id=ZONE_ID,
    )
    group_share = await rebac_service.share_with_group(
        RESOURCE,
        "analysts",
        permission="editor",
        zone_id=ZONE_ID,
    )

    user_tuples = await rebac_service.rebac_list_tuples(
        subject=("user", "bob"),
        relation="shared-viewer",
        object=RESOURCE,
    )
    assert [t["tuple_id"] for t in user_tuples] == [user_share["tuple_id"]]

    group_tuples = await rebac_service.rebac_list_tuples(
        subject=("group", "analysts"),
        relation="shared-editor",
        object=RESOURCE,
    )
    assert [t["tuple_id"] for t in group_tuples] == [group_share["tuple_id"]]
    assert group_tuples[0]["subject_relation"] == "member"

    assert await rebac_service.rebac_check(("user", "bob"), "read", RESOURCE, zone_id=ZONE_ID)

    outgoing = await rebac_service.list_outgoing_shares(RESOURCE, zone_id=ZONE_ID)
    assert {
        (share["recipient_id"], share["permission_level"])
        for share in outgoing
        if share["resource_id"] == RESOURCE[1]
    } >= {("bob", "viewer"), ("analysts", "editor")}

    incoming = await rebac_service.list_incoming_shares(("user", "bob"), zone_id=ZONE_ID)
    assert [(share["resource_id"], share["permission_level"]) for share in incoming] == [
        (RESOURCE[1], "viewer")
    ]

    group_incoming = await rebac_service.list_incoming_shares(
        ("group", "analysts"), zone_id=ZONE_ID
    )
    assert [(share["resource_id"], share["permission_level"]) for share in group_incoming] == [
        (RESOURCE[1], "editor")
    ]

    assert await rebac_service.revoke_share(
        RESOURCE,
        ("user", "bob"),
        permission="viewer",
        zone_id=ZONE_ID,
    )
    assert not await rebac_service.rebac_list_tuples(
        subject=("user", "bob"),
        relation="shared-viewer",
        object=RESOURCE,
    )
    assert not await rebac_service.rebac_check(("user", "bob"), "read", RESOURCE, zone_id=ZONE_ID)

    assert await rebac_service.revoke_share_by_id(group_share["tuple_id"])
    assert not await rebac_service.rebac_list_tuples(
        subject=("group", "analysts"),
        relation="shared-editor",
        object=RESOURCE,
    )


@pytest.mark.asyncio
async def test_rpc_public_private_consent_and_privacy_filtering_round_trip(
    rebac_service: ReBACService,
) -> None:
    await rebac_service.rebac_create(
        subject=("user", "alice"),
        relation="direct_viewer",
        object=RESOURCE,
        zone_id=ZONE_ID,
    )
    await rebac_service.rebac_create(
        subject=("user", "charlie"),
        relation="direct_viewer",
        object=RESOURCE,
        zone_id=ZONE_ID,
    )

    public = await rebac_service.make_public(RESOURCE, zone_id=ZONE_ID)
    assert public["tuple_id"]
    assert await rebac_service.rebac_list_tuples(
        subject=("*", "*"),
        relation="public_discoverable",
        object=RESOURCE,
    )

    assert await rebac_service.make_private(RESOURCE, zone_id=ZONE_ID)
    assert not await rebac_service.rebac_list_tuples(
        subject=("*", "*"),
        relation="public_discoverable",
        object=RESOURCE,
    )

    consent = await rebac_service.grant_consent(
        subject=("user", "alice"),
        target=("user", "bob"),
        zone_id=ZONE_ID,
    )
    assert consent["tuple_id"]
    assert await rebac_service.rebac_list_tuples(
        subject=("user", "bob"),
        relation="consent_granted",
        object=("user", "alice"),
    )

    visible = await rebac_service.rebac_expand_with_privacy(
        "read",
        RESOURCE,
        zone_id=ZONE_ID,
        requester=("user", "bob"),
    )
    assert ("user", "alice") in visible
    assert ("user", "charlie") not in visible

    assert await rebac_service.revoke_consent(
        subject=("user", "alice"),
        target=("user", "bob"),
        zone_id=ZONE_ID,
    )
    assert not await rebac_service.rebac_list_tuples(
        subject=("user", "bob"),
        relation="consent_granted",
        object=("user", "alice"),
    )


@pytest.mark.asyncio
async def test_rpc_dynamic_viewer_config_and_read_use_persisted_column_config(
    rebac_service: ReBACService,
) -> None:
    column_config = {
        "hidden_columns": ["ssn"],
        "visible_columns": ["name", "email"],
        "aggregations": {},
    }
    await rebac_service.rebac_create(
        subject=("user", "bob"),
        relation="dynamic_viewer",
        object=RESOURCE,
        zone_id=ZONE_ID,
        column_config=column_config,
    )

    config = await rebac_service.get_dynamic_viewer_config(
        RESOURCE,
        zone_id=ZONE_ID,
        subject=("user", "bob"),
    )
    assert config == column_config

    filtered = await rebac_service.read_with_dynamic_viewer(
        RESOURCE,
        "name,email,ssn\nAda,ada@example.com,123-45-6789\n",
        zone_id=ZONE_ID,
        subject=("user", "bob"),
    )
    assert filtered == "name,email\nAda,ada@example.com\n"


def _json_from_output(output: str) -> Any:
    return json.loads(output)


def test_cli_rebac_share_and_list_objects_use_real_service(cli_runner: CliRunner) -> None:
    share = cli_runner.invoke(
        rebac_cli.rebac,
        [
            "share",
            "user",
            "file",
            RESOURCE[1],
            "bob",
            "--permission",
            "viewer",
            "--zone-id",
            ZONE_ID,
            "--format",
            "json",
        ],
    )
    assert share.exit_code == 0, share.output
    share_payload = _json_from_output(share.output)
    assert share_payload["shared_with"] == "bob"

    incoming = cli_runner.invoke(
        rebac_cli.rebac,
        [
            "share",
            "incoming",
            "user",
            "bob",
            "--zone-id",
            ZONE_ID,
            "--format",
            "json",
        ],
    )
    assert incoming.exit_code == 0, incoming.output
    assert _json_from_output(incoming.output)["items"][0]["resource_id"] == RESOURCE[1]

    listed = cli_runner.invoke(
        rebac_cli.rebac,
        [
            "list-objects",
            "shared-viewer",
            "user",
            "bob",
            "--zone-id",
            ZONE_ID,
            "--format",
            "json",
        ],
    )
    assert listed.exit_code == 0, listed.output
    assert _json_from_output(listed.output)["objects"] == [["file", RESOURCE[1]]]

    revoke = cli_runner.invoke(
        rebac_cli.rebac,
        [
            "share",
            "revoke",
            "file",
            RESOURCE[1],
            "user",
            "bob",
            "--permission",
            "viewer",
            "--zone-id",
            ZONE_ID,
            "--format",
            "json",
        ],
    )
    assert revoke.exit_code == 0, revoke.output
    assert _json_from_output(revoke.output)["revoked"] is True

    empty = cli_runner.invoke(
        rebac_cli.rebac,
        [
            "share",
            "incoming",
            "user",
            "bob",
            "--zone-id",
            ZONE_ID,
            "--format",
            "json",
        ],
    )
    assert empty.exit_code == 0, empty.output
    assert _json_from_output(empty.output)["items"] == []


def test_cli_rebac_list_objects_accepts_remote_sync_proxy(
    remote_cli_runner: CliRunner,
) -> None:
    listed = remote_cli_runner.invoke(
        rebac_cli.rebac,
        [
            "list-objects",
            "shared-viewer",
            "user",
            "bob",
            "--zone-id",
            ZONE_ID,
            "--format",
            "json",
        ],
    )

    assert listed.exit_code == 0, listed.output
    assert _json_from_output(listed.output)["objects"] == [["file", RESOURCE[1]]]


def test_cli_dynamic_viewer_config_and_read_use_real_service(
    cli_runner: CliRunner,
    rebac_service: ReBACService,
    tmp_path,
) -> None:
    csv_path = tmp_path / "report.csv"
    csv_path.write_text("name,email,ssn\nAda,ada@example.com,123-45-6789\n")
    rebac_service.rebac_create_sync(
        subject=("user", "bob"),
        relation="dynamic_viewer",
        object=("file", str(csv_path)),
        zone_id=ZONE_ID,
        column_config={
            "hidden_columns": ["ssn"],
            "visible_columns": ["name", "email"],
            "aggregations": {},
        },
    )

    config = cli_runner.invoke(
        rebac_cli.rebac,
        [
            "dynamic",
            "config",
            "user",
            "bob",
            str(csv_path),
            "--zone-id",
            ZONE_ID,
            "--format",
            "json",
        ],
    )
    assert config.exit_code == 0, config.output
    assert _json_from_output(config.output)["column_config"]["hidden_columns"] == ["ssn"]

    read = cli_runner.invoke(
        rebac_cli.rebac,
        [
            "dynamic",
            "read",
            "user",
            "bob",
            str(csv_path),
            "--content-file",
            str(csv_path),
            "--zone-id",
            ZONE_ID,
            "--format",
            "json",
        ],
    )
    assert read.exit_code == 0, read.output
    assert _json_from_output(read.output)["filtered_data"] == "name,email\nAda,ada@example.com\n"
