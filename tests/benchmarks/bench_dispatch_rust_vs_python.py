"""Benchmarks: Rust kernel dispatch vs Python dispatch overhead.

Measures the performance of the §7 kernel boundary collapse:
- Hook dispatch (pre + post) via Rust InterceptHook trait
- Observer dispatch via Rust ObserverRegistry bitmask filtering
- sys_read / sys_write full syscall path with hooks

Run: python -m pytest tests/benchmarks/bench_dispatch_rust_vs_python.py -v --benchmark-only

Issue #1868: PyKernel Boundary Collapse.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.vfs_hooks import ReadHookContext, WriteHookContext
from nexus.core.nexus_fs_dispatch import DispatchMixin

try:
    from nexus_kernel import PyKernel

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False


# ── Fixtures ─────────────────────────────────────────────────────────


class _BenchDispatch(DispatchMixin):
    """Minimal DispatchMixin with Rust PyKernel for benchmarking."""

    def __init__(self):
        self._kernel = PyKernel()
        self._init_dispatch()


@pytest.fixture
def dispatch():
    return _BenchDispatch()


def _make_hook(name: str = "bench_hook"):
    """Create a minimal sync hook for benchmarking."""
    hook = MagicMock()
    hook.name = name
    hook.TRIE_PATTERN = None
    return hook


def _write_ctx(**kw):
    defaults = {
        "path": "/bench/file.txt",
        "content": b"x" * 1024,
        "context": None,
        "zone_id": "z1",
        "agent_id": None,
        "is_new_file": True,
        "content_id": "abc123",
        "metadata": None,
        "old_metadata": None,
        "new_version": 1,
    }
    defaults.update(kw)
    return WriteHookContext(**defaults)


def _read_ctx(**kw):
    defaults = {
        "path": "/bench/file.txt",
        "context": None,
        "zone_id": "z1",
        "agent_id": None,
        "content": b"x" * 1024,
        "content_id": "abc123",
    }
    defaults.update(kw)
    return ReadHookContext(**defaults)


# ── Pre-hook dispatch benchmarks ─────────────────────────────────────


@pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_kernel not available")
class TestPreHookDispatch:
    """Benchmark: Rust dispatch_pre_hooks overhead."""

    @pytest.mark.benchmark(group="pre_hook_dispatch")
    def test_pre_hook_no_hooks(self, benchmark, dispatch):
        """Baseline: dispatch_pre_hooks with zero hooks registered."""
        ctx = _write_ctx()
        benchmark(dispatch._kernel.dispatch_pre_hooks, "write", ctx)

    @pytest.mark.benchmark(group="pre_hook_dispatch")
    def test_pre_hook_one_hook(self, benchmark, dispatch):
        """dispatch_pre_hooks with 1 hook registered."""
        dispatch.register_intercept_write(_make_hook("h1"))
        ctx = _write_ctx()
        benchmark(dispatch._kernel.dispatch_pre_hooks, "write", ctx)

    @pytest.mark.benchmark(group="pre_hook_dispatch")
    def test_pre_hook_five_hooks(self, benchmark, dispatch):
        """dispatch_pre_hooks with 5 hooks registered."""
        for i in range(5):
            dispatch.register_intercept_write(_make_hook(f"h{i}"))
        ctx = _write_ctx()
        benchmark(dispatch._kernel.dispatch_pre_hooks, "write", ctx)


# ── Post-hook dispatch benchmarks ────────────────────────────────────


@pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_kernel not available")
class TestPostHookDispatch:
    """Benchmark: Rust dispatch_post_hooks overhead."""

    @pytest.mark.benchmark(group="post_hook_dispatch")
    def test_post_hook_no_hooks(self, benchmark, dispatch):
        """Baseline: dispatch_post_hooks with zero hooks."""
        ctx = _write_ctx()
        benchmark(dispatch._kernel.dispatch_post_hooks, "write", ctx)

    @pytest.mark.benchmark(group="post_hook_dispatch")
    def test_post_hook_one_hook(self, benchmark, dispatch):
        """dispatch_post_hooks with 1 sync hook."""
        dispatch.register_intercept_write(_make_hook("h1"))
        ctx = _write_ctx()
        benchmark(dispatch._kernel.dispatch_post_hooks, "write", ctx)

    @pytest.mark.benchmark(group="post_hook_dispatch")
    def test_post_hook_five_hooks(self, benchmark, dispatch):
        """dispatch_post_hooks with 5 sync hooks."""
        for i in range(5):
            dispatch.register_intercept_write(_make_hook(f"h{i}"))
        ctx = _write_ctx()
        benchmark(dispatch._kernel.dispatch_post_hooks, "write", ctx)


# ── Observer dispatch benchmarks ─────────────────────────────────────


def _make_observer(name: str = "bench_obs", event_mask: int = 0x7FF):
    """Create a minimal sync observer."""

    class _Obs:
        def __init__(self):
            self.event_mask = event_mask

        def on_mutation(self, event):
            pass

    obs = _Obs()
    obs.__class__.__name__ = name
    return obs


@pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_kernel not available")
class TestObserverDispatch:
    """Benchmark: Rust dispatch_observers bitmask filtering."""

    @pytest.mark.benchmark(group="observer_dispatch")
    def test_dispatch_observers_no_observers(self, benchmark, dispatch):
        """Baseline: dispatch_observers with zero observers."""
        benchmark(dispatch._kernel.dispatch_observers, 0x001)

    @pytest.mark.benchmark(group="observer_dispatch")
    def test_dispatch_observers_one_matching(self, benchmark, dispatch):
        """dispatch_observers with 1 matching observer."""
        dispatch.register_observe(_make_observer("o1", event_mask=0x001))
        benchmark(dispatch._kernel.dispatch_observers, 0x001)

    @pytest.mark.benchmark(group="observer_dispatch")
    def test_dispatch_observers_five_mixed(self, benchmark, dispatch):
        """dispatch_observers with 5 observers, 2 matching."""
        dispatch.register_observe(_make_observer("o1", event_mask=0x001))
        dispatch.register_observe(_make_observer("o2", event_mask=0x002))
        dispatch.register_observe(_make_observer("o3", event_mask=0x001))
        dispatch.register_observe(_make_observer("o4", event_mask=0x004))
        dispatch.register_observe(_make_observer("o5", event_mask=0x008))
        benchmark(dispatch._kernel.dispatch_observers, 0x001)


# ── Full syscall benchmarks (sys_read / sys_write) ───────────────────


@pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_kernel not available")
class TestSyscallOverhead:
    """Benchmark: full sys_read/sys_write path including hook dispatch.

    Uses CAS backend mounted at /bench.
    """

    @pytest.fixture
    def mounted_dispatch(self, tmp_path):
        """Dispatch with CAS backend mounted at /bench."""
        d = _BenchDispatch()
        # F4 Rust-ification: mount via sys_setattr(DT_MOUNT) instead of legacy add_mount.
        d._kernel.sys_setattr(
            path="/bench",
            entry_type=2,  # DT_MOUNT
            backend_name="local",
            local_root=str(tmp_path / "cas"),
            fsync=False,
            backend_type="cas",
            readonly=False,
            admin_only=False,
            zone_id=ROOT_ZONE_ID,
        )
        return d

    @pytest.mark.benchmark(group="syscall")
    @pytest.mark.benchmark_ci
    def test_sys_write_no_hooks(self, benchmark, mounted_dispatch):
        """sys_write 1KB, no hooks."""
        from nexus_kernel import PyOperationContext

        ctx = PyOperationContext(user_id="bench", zone_id=ROOT_ZONE_ID)
        content = b"x" * 1024

        def _write():
            mounted_dispatch._kernel.sys_write("/bench/file.txt", ctx, content)

        benchmark(_write)

    @pytest.mark.benchmark(group="syscall")
    @pytest.mark.benchmark_ci
    def test_sys_read_no_hooks(self, benchmark, mounted_dispatch):
        """sys_read 1KB, no hooks (after write)."""
        from nexus_kernel import PyOperationContext

        ctx = PyOperationContext(user_id="bench", zone_id=ROOT_ZONE_ID)
        mounted_dispatch._kernel.sys_write("/bench/read_target.txt", ctx, b"y" * 1024)

        def _read():
            mounted_dispatch._kernel.sys_read("/bench/read_target.txt", ctx)

        benchmark(_read)

    @pytest.mark.benchmark(group="syscall")
    def test_sys_write_with_hooks(self, benchmark, mounted_dispatch):
        """sys_write 1KB with 3 hooks (pre + post)."""
        from nexus_kernel import PyOperationContext

        for i in range(3):
            mounted_dispatch.register_intercept_write(_make_hook(f"h{i}"))

        ctx = PyOperationContext(user_id="bench", zone_id=ROOT_ZONE_ID)
        content = b"x" * 1024

        def _write():
            mounted_dispatch._kernel.sys_write("/bench/hooked.txt", ctx, content)

        benchmark(_write)

    @pytest.mark.benchmark(group="syscall")
    def test_sys_read_with_hooks(self, benchmark, mounted_dispatch):
        """sys_read 1KB with 3 hooks (pre only for read)."""
        from nexus_kernel import PyOperationContext

        for i in range(3):
            mounted_dispatch.register_intercept_read(_make_hook(f"h{i}"))

        ctx = PyOperationContext(user_id="bench", zone_id=ROOT_ZONE_ID)
        mounted_dispatch._kernel.sys_write("/bench/hooked_read.txt", ctx, b"z" * 1024)

        def _read():
            mounted_dispatch._kernel.sys_read("/bench/hooked_read.txt", ctx)

        benchmark(_read)
