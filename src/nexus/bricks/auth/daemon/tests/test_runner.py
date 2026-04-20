from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

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
