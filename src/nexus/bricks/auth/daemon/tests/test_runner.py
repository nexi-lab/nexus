from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from nexus.bricks.auth.daemon.adapters import SubprocessSource
from nexus.bricks.auth.daemon.queue import PushQueue
from nexus.bricks.auth.daemon.runner import DaemonRunner, _codex_account_from_auth_json


def test_next_refresh_wait_prefers_token_expiry(tmp_path: Path) -> None:
    """When expiry_provider returns < jwt_refresh_every, scheduler shortens wait."""
    queue = PushQueue(tmp_path / "queue.db")
    pusher = MagicMock()
    # Token expires in 120s; with 60s margin, expected proactive = 60s.
    runner = DaemonRunner(
        source_watch_target=tmp_path / "auth.json",
        queue=queue,
        pusher=pusher,
        jwt_refresh_every=45 * 60,
        status_path=tmp_path / "status.json",
        jwt_refresh_callable=lambda: None,
        jwt_expiry_provider=lambda: 120.0,
        jwt_refresh_margin_s=60,
    )
    wait = runner._next_refresh_wait_s()
    # Base is 60s (120 - margin), jitter ±10% → [54, 66], floored at 1.
    assert 50.0 <= wait <= 70.0


def test_next_refresh_wait_falls_back_to_fixed(tmp_path: Path) -> None:
    """When expiry_provider is None, scheduler uses jwt_refresh_every."""
    queue = PushQueue(tmp_path / "queue.db")
    pusher = MagicMock()
    runner = DaemonRunner(
        source_watch_target=tmp_path / "auth.json",
        queue=queue,
        pusher=pusher,
        jwt_refresh_every=600,
        status_path=tmp_path / "status.json",
        jwt_refresh_callable=lambda: None,
    )
    wait = runner._next_refresh_wait_s()
    # Jitter ±10% of 600 → [540, 660].
    assert 540.0 <= wait <= 660.0


def test_next_refresh_wait_floors_at_60s(tmp_path: Path) -> None:
    """A near-expired token floors at 60s so we don't thrash the refresh endpoint."""
    queue = PushQueue(tmp_path / "queue.db")
    pusher = MagicMock()
    runner = DaemonRunner(
        source_watch_target=tmp_path / "auth.json",
        queue=queue,
        pusher=pusher,
        jwt_refresh_every=45 * 60,
        status_path=tmp_path / "status.json",
        jwt_refresh_callable=lambda: None,
        jwt_expiry_provider=lambda: 1.0,  # already essentially expired
        jwt_refresh_margin_s=60,
    )
    wait = runner._next_refresh_wait_s()
    # Floor is 60s; jitter ±10% → [54, 66].
    assert 50.0 <= wait <= 70.0


def _jwt_with_claims(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def test_codex_account_extract_from_id_token() -> None:
    token = _jwt_with_claims({"email": "alice@example.com", "sub": "u-1"})
    content = json.dumps({"tokens": {"id_token": token}}).encode()
    assert _codex_account_from_auth_json(content) == "alice@example.com"


def test_codex_account_falls_back_to_sub_when_email_missing() -> None:
    token = _jwt_with_claims({"sub": "user-abc"})
    content = json.dumps({"tokens": {"id_token": token}}).encode()
    assert _codex_account_from_auth_json(content) == "user-abc"


def test_codex_account_top_level_email() -> None:
    """API-key-only codex setups have no id_token; use top-level email."""
    content = json.dumps({"OPENAI_API_KEY": "sk-x", "email": "bob@example.com"}).encode()
    assert _codex_account_from_auth_json(content) == "bob@example.com"


def test_codex_account_none_when_undecodable() -> None:
    """Garbage content must return None so the caller skips pushing."""
    assert _codex_account_from_auth_json(b"not json") is None
    assert _codex_account_from_auth_json(b"{}") is None


def test_retry_pending_loop_replays_watched_file(tmp_path: Path) -> None:
    """Pending queue row → retry thread re-reads source + calls push_source."""
    queue = PushQueue(tmp_path / "queue.db")
    # Seed queue with a pending row (simulates a prior push that failed).
    queue.enqueue("codex/alice@example.com", payload_hash="pending-hash")
    auth_file = tmp_path / "auth.json"
    token = _jwt_with_claims({"email": "alice@example.com"})
    auth_file.write_bytes(json.dumps({"tokens": {"id_token": token}}).encode())

    pusher = MagicMock()
    runner = DaemonRunner(
        source_watch_target=auth_file,
        queue=queue,
        pusher=pusher,
        jwt_refresh_every=9999,
        status_path=tmp_path / "status.json",
        retry_pending_every=1,  # tight interval for test
    )
    t = threading.Thread(target=runner.run, daemon=True)
    t.start()
    for _ in range(60):
        if pusher.push_source.called:
            break
        time.sleep(0.05)
    runner.shutdown()
    t.join(timeout=5.0)
    # Retry loop must have invoked push_source at least once for the codex src.
    assert pusher.push_source.called
    call = pusher.push_source.call_args
    assert call.args[0] == "codex"
    assert call.kwargs["account_identifier"] == "alice@example.com"


def test_retry_pending_loop_disabled_when_interval_zero(tmp_path: Path) -> None:
    """retry_pending_every=0 disables the loop entirely."""
    queue = PushQueue(tmp_path / "queue.db")
    queue.enqueue("codex/alice@example.com", payload_hash="pending-hash")
    auth_file = tmp_path / "auth.json"
    auth_file.write_bytes(b'{"OPENAI_API_KEY":"sk-x"}')  # no id_token → account None

    pusher = MagicMock()
    runner = DaemonRunner(
        source_watch_target=auth_file,
        queue=queue,
        pusher=pusher,
        jwt_refresh_every=9999,
        status_path=tmp_path / "status.json",
        retry_pending_every=0,  # disable
    )
    assert runner._retry_pending_every == 0


def test_startup_drain_no_watch_target_is_noop(tmp_path: Path) -> None:
    """Missing watch file must NOT crash the daemon — just log and continue."""
    queue = PushQueue(tmp_path / "queue.db")
    queue.enqueue("codex/u@x", payload_hash="hashA")

    pusher = MagicMock()
    runner = DaemonRunner(
        source_watch_target=tmp_path / "absent.json",
        queue=queue,
        pusher=pusher,
        jwt_refresh_every=9999,
        status_path=tmp_path / "status.json",
    )
    runner.drain_startup()
    # Row stays pending because we had no content to replay.
    assert queue.list_pending()[0].profile_id == "codex/u@x"
    pusher.push_source.assert_not_called()


def test_startup_drain_replays_watch_target(tmp_path: Path) -> None:
    """With a present file, drain_startup re-pushes it so pending rows clear."""
    queue = PushQueue(tmp_path / "queue.db")
    auth_file = tmp_path / "auth.json"
    token = _jwt_with_claims({"email": "alice@example.com"})
    auth_file.write_bytes(json.dumps({"tokens": {"id_token": token}}).encode())

    pusher = MagicMock()
    runner = DaemonRunner(
        source_watch_target=auth_file,
        queue=queue,
        pusher=pusher,
        jwt_refresh_every=9999,
        status_path=tmp_path / "status.json",
    )
    runner.drain_startup()
    pusher.push_source.assert_called_once_with(
        "codex",
        content=auth_file.read_bytes(),
        provider="codex",
        account_identifier="alice@example.com",
    )


def test_subprocess_sources_pushed_on_startup(tmp_path: Path) -> None:
    """Runner's subprocess poll thread fires one fetch+push cycle immediately."""
    queue = PushQueue(tmp_path / "queue.db")
    pusher = MagicMock()
    # Use `sh -c` so fetch() returns stable bytes on any POSIX box. Provide
    # an account_cmd that yields a deterministic label so the runner doesn't
    # skip the push under the new "account_identifier required" policy.
    src = SubprocessSource(
        name="gcloud",
        cmd=("sh", "-c", "printf ya29.fake-token"),
        account_cmd=("sh", "-c", "printf u@example.com"),
    )
    runner = DaemonRunner(
        source_watch_target=tmp_path / "auth.json",
        queue=queue,
        pusher=pusher,
        jwt_refresh_every=9999,
        status_path=tmp_path / "status.json",
        subprocess_sources=(src,),
        subprocess_poll_every=9999,
    )
    t = threading.Thread(target=runner.run, daemon=True)
    t.start()
    for _ in range(50):
        if pusher.push_source.called:
            break
        time.sleep(0.05)
    runner.shutdown()
    t.join(timeout=5.0)
    pusher.push_source.assert_any_call(
        "gcloud", content=b"ya29.fake-token", account_identifier="u@example.com"
    )


def test_subprocess_source_without_account_cmd_skips_push(tmp_path: Path) -> None:
    """fetch succeeds but account_cmd is None → refuse to push with 'unknown'."""
    queue = PushQueue(tmp_path / "queue.db")
    pusher = MagicMock()
    src = SubprocessSource(name="gcloud", cmd=("sh", "-c", "printf tok"))  # no account_cmd
    runner = DaemonRunner(
        source_watch_target=tmp_path / "auth.json",
        queue=queue,
        pusher=pusher,
        jwt_refresh_every=9999,
        status_path=tmp_path / "status.json",
        subprocess_sources=(src,),
        subprocess_poll_every=9999,
    )
    t = threading.Thread(target=runner.run, daemon=True)
    t.start()
    time.sleep(0.3)
    runner.shutdown()
    t.join(timeout=5.0)
    pusher.push_source.assert_not_called()


def test_subprocess_source_unavailable_skips_push(tmp_path: Path) -> None:
    """If fetch returns None (binary missing, cmd failed), no push happens."""
    queue = PushQueue(tmp_path / "queue.db")
    pusher = MagicMock()
    src = SubprocessSource(name="gcloud", cmd=("nonexistent-binary",))
    runner = DaemonRunner(
        source_watch_target=tmp_path / "auth.json",
        queue=queue,
        pusher=pusher,
        jwt_refresh_every=9999,
        status_path=tmp_path / "status.json",
        subprocess_sources=(src,),
        subprocess_poll_every=9999,
    )
    t = threading.Thread(target=runner.run, daemon=True)
    t.start()
    time.sleep(0.3)
    runner.shutdown()
    t.join(timeout=5.0)
    pusher.push_source.assert_not_called()


def test_sigterm_stops_cleanly(tmp_path: Path) -> None:
    queue = PushQueue(tmp_path / "queue.db")
    pusher = MagicMock()
    runner = DaemonRunner(
        source_watch_target=tmp_path / "auth.json",
        queue=queue,
        pusher=pusher,
        jwt_refresh_every=9999,
        status_path=tmp_path / "status.json",
    )
    t = threading.Thread(target=runner.run, daemon=True)
    t.start()
    time.sleep(0.5)
    runner.shutdown()
    t.join(timeout=5.0)
    assert not t.is_alive()
    assert runner.status().state in ("stopped", "healthy", "degraded")
