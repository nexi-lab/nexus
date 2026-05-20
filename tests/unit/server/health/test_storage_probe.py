"""Tests for the storage health probe."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import sleep
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.health.probes import router

_STORAGE_AUTH_HEADERS = {"Authorization": "Bearer test-storage-key"}


def _make_storage_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.startup_tracker = None
    app.state.nexus_fs = None
    app.state.api_key = "test-storage-key"
    app.state.auth_provider = None
    return app


def test_storage_probe_requires_admin_auth() -> None:
    client = TestClient(_make_storage_app())

    resp = client.get("/healthz/storage")

    assert resp.status_code == 401


def test_storage_probe_succeeds_when_round_trip_succeeds() -> None:
    app = _make_storage_app()
    mock_fs = MagicMock()
    mock_fs.read.return_value = b"nexus-healthz-storage-probe"
    app.state.nexus_fs = mock_fs

    client = TestClient(app)
    resp = client.get("/healthz/storage", headers=_STORAGE_AUTH_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"

    write_kwargs = mock_fs.write.call_args.kwargs
    probe_path = write_kwargs["path"]
    context = write_kwargs["context"]
    assert probe_path.startswith("/__healthz__/")
    assert write_kwargs["buf"] == b"nexus-healthz-storage-probe"
    assert context.is_system is True

    mock_fs.read.assert_called_once_with(probe_path, context=context)
    mock_fs.sys_unlink.assert_called_once_with(probe_path, context=context)


def test_storage_probe_503_when_nexus_fs_missing() -> None:
    client = TestClient(_make_storage_app())

    resp = client.get("/healthz/storage", headers=_STORAGE_AUTH_HEADERS)

    assert resp.status_code == 503
    assert resp.json() == {
        "status": "unhealthy",
        "reason": "nexus_fs_unavailable",
    }


def test_storage_probe_503_when_storage_write_fails() -> None:
    app = _make_storage_app()
    mock_fs = MagicMock()
    mock_fs.write.side_effect = OSError(28, "No space left on device")
    app.state.nexus_fs = mock_fs

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/healthz/storage", headers=_STORAGE_AUTH_HEADERS)

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["reason"] == "storage_probe_failed"
    assert "No space left on device" in body["error"]


def test_storage_probe_503_when_readback_does_not_match() -> None:
    app = _make_storage_app()
    mock_fs = MagicMock()
    mock_fs.read.return_value = b"wrong"
    app.state.nexus_fs = mock_fs

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/healthz/storage", headers=_STORAGE_AUTH_HEADERS)

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["reason"] == "storage_probe_failed"
    assert body["error"] == "storage probe readback mismatch"
    mock_fs.sys_unlink.assert_called_once()


def test_storage_probe_cleanup_runs_after_read_failure() -> None:
    app = _make_storage_app()
    mock_fs = MagicMock()
    mock_fs.read.side_effect = RuntimeError("read failed")
    app.state.nexus_fs = mock_fs

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/healthz/storage", headers=_STORAGE_AUTH_HEADERS)

    assert resp.status_code == 503
    body = resp.json()
    assert body["reason"] == "storage_probe_failed"
    assert body["error"] == "read failed"
    mock_fs.sys_unlink.assert_called_once()


def test_storage_probe_503_when_cleanup_fails_after_successful_readback() -> None:
    app = _make_storage_app()
    mock_fs = MagicMock()
    mock_fs.read.return_value = b"nexus-healthz-storage-probe"
    mock_fs.sys_unlink.side_effect = RuntimeError("delete failed")
    app.state.nexus_fs = mock_fs

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/healthz/storage", headers=_STORAGE_AUTH_HEADERS)

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["reason"] == "storage_probe_failed"
    assert body["error"] == "storage probe cleanup failed: delete failed"


def test_storage_probe_503_when_probe_already_in_progress(monkeypatch) -> None:
    app = _make_storage_app()
    mock_fs = MagicMock()
    write_started = Event()
    release_write = Event()
    counter_lock = Lock()
    write_calls = 0

    def blocking_write(**_: object) -> None:
        nonlocal write_calls
        with counter_lock:
            write_calls += 1
        write_started.set()
        release_write.wait(timeout=5)

    mock_fs.write.side_effect = blocking_write
    mock_fs.read.return_value = b"nexus-healthz-storage-probe"
    app.state.nexus_fs = mock_fs
    monkeypatch.setenv("NEXUS_HEALTHZ_STORAGE_TIMEOUT_SECONDS", "0.2")

    client1 = TestClient(app, raise_server_exceptions=False)
    client2 = TestClient(app, raise_server_exceptions=False)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first_probe = executor.submit(
            client1.get,
            "/healthz/storage",
            headers=_STORAGE_AUTH_HEADERS,
        )
        assert write_started.wait(timeout=1)

        resp = client2.get("/healthz/storage", headers=_STORAGE_AUTH_HEADERS)
        sleep(0.3)
        release_write.set()
        first_resp = first_probe.result(timeout=2)

    assert first_resp.status_code == 503
    assert first_resp.json()["reason"] == "storage_probe_timeout"
    assert resp.status_code == 503
    assert resp.json() == {
        "status": "unhealthy",
        "reason": "storage_probe_in_progress",
    }
    assert write_calls == 1
