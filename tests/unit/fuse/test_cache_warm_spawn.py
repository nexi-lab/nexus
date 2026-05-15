"""Unit tests for NexusFUSEOperations._kickoff_cache_warm — production hydration trigger (Issue #4055)."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from nexus.fuse.operations import NexusFUSEOperations


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestKickoffCacheWarm:
    def test_kickoff_calls_cache_warm_with_wait_false(self) -> None:
        ops = NexusFUSEOperations.__new__(NexusFUSEOperations)
        rust_client = MagicMock()
        rust_client.cache_warm.return_value = {"started": True}

        ops._kickoff_cache_warm(rust_client)

        assert _wait_for(lambda: rust_client.cache_warm.call_count >= 1), (
            "cache_warm was never called"
        )
        # Production must use wait=False so the foreground RPC socket isn't
        # held for the whole hydration window.
        rust_client.cache_warm.assert_called_with("/", wait=False)

    def test_returns_immediately_without_blocking(self) -> None:
        ops = NexusFUSEOperations.__new__(NexusFUSEOperations)
        rust_client = MagicMock()
        block = threading.Event()
        rust_client.cache_warm.side_effect = lambda *_a, **_kw: block.wait(timeout=5)

        start = time.monotonic()
        ops._kickoff_cache_warm(rust_client)
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, f"_kickoff_cache_warm took {elapsed:.3f}s — appears synchronous"
        block.set()

    def test_swallows_exceptions_silently(self) -> None:
        ops = NexusFUSEOperations.__new__(NexusFUSEOperations)
        rust_client = MagicMock()
        rust_client.cache_warm.side_effect = RuntimeError("daemon dead")

        ops._kickoff_cache_warm(rust_client)
        assert _wait_for(lambda: rust_client.cache_warm.call_count >= 1), (
            "cache_warm was never called"
        )
        # No crash here — that's the point.


class TestScopedMountDoesNotStartRustDaemon:
    """A scoped/agent mount (context!=None) must not construct RustFUSEClient.

    The Rust daemon's Unix socket has no per-request OperationContext, so a
    daemon spawned with the owner API key would bypass ReBAC for any same-UID
    caller. Issue #4055 R4 forces use_rust=False before construction.
    """

    def test_context_present_skips_rust_client_construction(self) -> None:
        from unittest.mock import patch

        from nexus.contracts.types import OperationContext

        # Build a minimal nexus_fs that WOULD satisfy the rust_client gate
        # if construction were reached.
        nexus_fs = MagicMock()
        nexus_fs._base_url = "http://nx.test"
        nexus_fs._api_key = "secret-owner-key"
        nexus_fs.zone_id = None

        scoped_ctx = OperationContext(
            user_id="agent-1",
            groups=[],
            is_admin=False,
            agent_id="agent-1",
            zone_id="zone-x",
        )

        with patch("nexus.fuse.rust_client.RustFUSEClient") as ctor:
            try:
                NexusFUSEOperations(
                    nexus_fs=nexus_fs,
                    mode=MagicMock(),
                    use_rust=True,
                    context=scoped_ctx,
                )
            except Exception:
                # __init__ may fail later for unrelated dependency reasons —
                # we only care that RustFUSEClient was never constructed.
                pass
            ctor.assert_not_called()
