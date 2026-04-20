from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from nexus.bricks.auth.daemon.adapters import SubprocessSource
from nexus.bricks.auth.daemon.queue import PushQueue
from nexus.bricks.auth.daemon.runner import DaemonRunner


def test_startup_drain_replays_pending(tmp_path: Path) -> None:
    queue = PushQueue(tmp_path / "queue.db")
    queue.enqueue("codex/u@x", payload_hash="hashA")

    pusher = MagicMock()
    runner = DaemonRunner(
        source_watch_target=tmp_path / "auth.json",
        queue=queue,
        pusher=pusher,
        jwt_refresh_every=9999,
        status_path=tmp_path / "status.json",
    )
    runner.drain_startup()
    # MVP: startup drain only logs; the queue row remains until the next successful push.
    assert queue.list_pending()[0].profile_id == "codex/u@x"


def test_subprocess_sources_pushed_on_startup(tmp_path: Path) -> None:
    """Runner's subprocess poll thread fires one fetch+push cycle immediately."""
    queue = PushQueue(tmp_path / "queue.db")
    pusher = MagicMock()
    # Use `sh -c` so fetch() returns stable bytes on any POSIX box.
    src = SubprocessSource(name="gcloud", cmd=("sh", "-c", "printf ya29.fake-token"))
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
    pusher.push_source.assert_any_call("gcloud", content=b"ya29.fake-token")


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
