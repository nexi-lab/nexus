"""Lifespan boot-time memory tweaks (Issue #3997)."""

import asyncio
import gc
import threading
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_apply_boot_tweaks_sets_stack_size_and_gc_threshold():
    """_apply_boot_tweaks sets 1 MB stack and adjusted GC threshold.

    Python 3.14 reduced GC to two generations; the third threshold value
    is always 0 in get_threshold() output regardless of what was passed.
    """
    from nexus.server.lifespan import _apply_boot_tweaks

    orig_stack = threading.stack_size()
    orig_thresh = gc.get_threshold()
    try:
        _apply_boot_tweaks()
        assert threading.stack_size() == 1 << 20
        # Python 3.14: third element is always 0 (obsolete generation)
        thresh = gc.get_threshold()
        assert thresh[0] == 50_000
        assert thresh[1] == 10
    finally:
        # Restore for other tests
        threading.stack_size(orig_stack)
        gc.set_threshold(*orig_thresh)


@pytest.mark.asyncio
async def test_idle_trimmer_invokes_gc_and_malloc_trim():
    """_idle_trimmer calls gc.collect + libc.malloc_trim per tick."""
    from nexus.server.lifespan import _idle_trimmer

    fake_libc = MagicMock()
    fake_libc.malloc_trim = MagicMock(return_value=1)

    with (
        patch("ctypes.CDLL", return_value=fake_libc),
        patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError()]),
        patch("gc.collect") as mock_collect,
    ):
        with pytest.raises(asyncio.CancelledError):
            await _idle_trimmer()
        assert mock_collect.call_count >= 1
        assert fake_libc.malloc_trim.call_count >= 1


@pytest.mark.asyncio
async def test_idle_trimmer_disabled_when_libc_unavailable():
    """_idle_trimmer exits cleanly when libc.so.6 not loadable (musl/Alpine)."""
    from nexus.server.lifespan import _idle_trimmer

    with patch("ctypes.CDLL", side_effect=OSError("no libc")):
        # Should return normally without hanging or raising
        await asyncio.wait_for(_idle_trimmer(), timeout=1.0)


@pytest.mark.asyncio
async def test_idle_trimmer_disabled_when_malloc_trim_missing():
    """_idle_trimmer exits when symbol absent (not glibc)."""
    from nexus.server.lifespan import _idle_trimmer

    # Plain object with no malloc_trim attribute → AttributeError on probe.
    # MagicMock auto-creates attributes, so we need a real object.
    class _FakeLibc:
        pass

    with patch("ctypes.CDLL", return_value=_FakeLibc()):
        await asyncio.wait_for(_idle_trimmer(), timeout=1.0)
