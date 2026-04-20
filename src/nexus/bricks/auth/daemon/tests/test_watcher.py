from __future__ import annotations

import time
from pathlib import Path
from threading import Event

from nexus.bricks.auth.daemon.watcher import SourceWatcher


def test_debounced_fire_once_on_rapid_writes(tmp_path: Path) -> None:
    target = tmp_path / "auth.json"
    target.write_text("{}")
    fired = Event()
    payloads: list[bytes] = []

    def on_change(_path: Path, content: bytes) -> None:
        payloads.append(content)
        fired.set()

    watcher = SourceWatcher(target, on_change=on_change, debounce_ms=200)
    watcher.start()
    try:
        for i in range(5):
            target.write_text(f'{{"v":{i}}}')
            time.sleep(0.02)
        assert fired.wait(timeout=2.0), "debounced callback never fired"
        time.sleep(0.4)  # give stragglers a beat to settle
    finally:
        watcher.stop()

    assert len(payloads) == 1, f"expected 1 callback, got {len(payloads)}"
    assert payloads[0] == b'{"v":4}'


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    target = tmp_path / "absent.json"
    watcher = SourceWatcher(target, on_change=lambda _p, _b: None, debounce_ms=100)
    watcher.start()
    try:
        time.sleep(0.3)
    finally:
        watcher.stop()
