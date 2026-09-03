"""CLI surfaces for fenced projections (#4738).

``nexus ops wait --seq N`` fences on a write's projection sequence over REST;
``nexus admin fs reconcile-projections`` repairs the projection — locally when
the CLI holds the RecordStore, otherwise through the admin REST route.
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import AsyncIterator, Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from nexus.cli.commands.admin import admin
from nexus.cli.commands.operations import ops_group
from nexus.storage.record_store import SQLAlchemyRecordStore

MOCK_URL = "http://localhost:2026"
_ENV = {"NEXUS_NO_AUTO_JSON": "1", "NEXUS_URL": MOCK_URL}


def _client(**method_returns: Any) -> MagicMock:
    client = MagicMock()
    for name, value in method_returns.items():
        setattr(client, name, MagicMock(return_value=value))
    return client


@pytest.fixture
def record_store() -> Generator[SQLAlchemyRecordStore, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        rs = SQLAlchemyRecordStore(db_path=Path(tmpdir) / "metadata.db")
        yield rs
        rs.close()


# ── nexus ops wait ────────────────────────────────────────────────────


def test_ops_wait_applied_exits_zero() -> None:
    fake = _client(
        get={"seq": 42, "applied": True, "latest_seq": 45, "zone_id": "eng", "waited_ms": 3}
    )
    with patch("nexus.cli.api_client.get_api_client_from_options", return_value=fake):
        result = CliRunner(env=_ENV).invoke(
            ops_group, ["wait", "--seq", "42", "--remote-url", MOCK_URL]
        )
    assert result.exit_code == 0, result.output
    fake.get.assert_called_once_with(
        "/api/v2/operations/wait", params={"seq": 42, "timeout_ms": 5000}
    )
    assert "Applied" in result.output and "42" in result.output and "latest_seq=45" in result.output


def test_ops_wait_412_exits_one_with_latest_seq() -> None:
    request = httpx.Request("GET", f"{MOCK_URL}/api/v2/operations/wait")
    response = httpx.Response(
        412,
        request=request,
        json={
            "detail": {
                "error": "projection_not_applied",
                "seq": 99,
                "latest_seq": 40,
                "zone_id": "eng",
                "waited_ms": 250,
            }
        },
    )
    fake = MagicMock()
    fake.get = MagicMock(
        side_effect=httpx.HTTPStatusError("412", request=request, response=response)
    )
    with patch("nexus.cli.api_client.get_api_client_from_options", return_value=fake):
        result = CliRunner(env=_ENV).invoke(
            ops_group,
            ["wait", "--seq", "99", "--timeout-ms", "250", "--remote-url", MOCK_URL],
        )
    assert result.exit_code == 1
    assert "Not applied" in result.output and "latest_seq=40" in result.output


def test_ops_wait_requires_seq() -> None:
    result = CliRunner(env=_ENV).invoke(ops_group, ["wait", "--remote-url", MOCK_URL])
    assert result.exit_code == 2


# ── nexus admin fs reconcile-projections ──────────────────────────────


def _open_filesystem_yielding(nx: Any) -> Any:
    @contextlib.asynccontextmanager
    async def _cm(*_a: Any, **_k: Any) -> AsyncIterator[Any]:
        yield nx

    return _cm


def test_reconcile_uses_rest_when_the_cli_has_no_record_store() -> None:
    report = {
        "prefix": "/ws",
        "zone_id": "eng",
        "dry_run": True,
        "scanned": 3,
        "in_sync": 1,
        "created": 1,
        "repaired": 1,
        "retired": 0,
        "stale_kernel": 0,
        "errors": 0,
        "truncated": False,
        "duration_ms": 12,
        "created_paths": ["/ws/new.txt"],
        "repaired_paths": ["/ws/lost.txt"],
        "retired_paths": [],
        "stale_kernel_paths": [],
        "error_messages": [],
    }
    fake = _client(post=report)
    opened: list[Any] = []

    @contextlib.asynccontextmanager
    async def _must_not_open(*a: Any, **k: Any) -> AsyncIterator[Any]:
        opened.append((a, k))
        yield SimpleNamespace(record_store=None)

    with (
        patch("nexus.cli.utils.open_filesystem", _must_not_open),
        patch("nexus.cli.api_client.get_api_client_from_options", return_value=fake),
    ):
        result = CliRunner(env=_ENV).invoke(
            admin,
            [
                "fs",
                "reconcile-projections",
                "--prefix",
                "/ws",
                "--dry-run",
                "--remote-url",
                MOCK_URL,
            ],
        )
    assert result.exit_code == 0, result.output
    fake.post.assert_called_once_with(
        "/api/v2/admin/reconcile-projections",
        json_body={"prefix": "/ws", "dry_run": True, "retire_missing": False, "max_entries": None},
    )
    assert opened == [], "a configured server must not spawn a local filesystem/kernel"
    assert "/ws/new.txt" in result.output and "/ws/lost.txt" in result.output
    assert "Dry run" in result.output


def test_reconcile_runs_locally_when_the_cli_holds_the_record_store(
    record_store: SQLAlchemyRecordStore,
) -> None:
    entries = [
        {
            "path": "/ws/a.txt",
            "size": 1,
            "content_id": "ca",
            "mime_type": "text/plain",
            "version": 1,
            "gen": 1,
            "entry_type": 0,
        }
    ]
    local_nx = SimpleNamespace(
        record_store=record_store,
        _zone_id="root",
        sys_readdir=lambda path, **kw: list(entries),
    )
    rest = MagicMock()
    no_server = SimpleNamespace(url=None, api_key=None)
    with (
        patch("nexus.cli.config.resolve_connection", return_value=no_server),
        patch("nexus.cli.utils.open_filesystem", _open_filesystem_yielding(local_nx)),
        patch("nexus.cli.api_client.get_api_client_from_options", return_value=rest),
    ):
        result = CliRunner(env={"NEXUS_NO_AUTO_JSON": "1"}).invoke(
            admin, ["fs", "reconcile-projections", "--prefix", "/ws"]
        )
    assert result.exit_code == 0, result.output
    rest.post.assert_not_called()
    assert "created" in result.output and "/ws/a.txt" in result.output

    from sqlalchemy import select

    from nexus.storage.models import FilePathModel

    with record_store.session_factory() as session:
        rows = session.execute(select(FilePathModel.virtual_path)).scalars().all()
    assert rows == ["/ws/a.txt"]
