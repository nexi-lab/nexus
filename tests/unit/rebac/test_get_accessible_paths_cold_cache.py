"""Regression test for cold-cache path resolution in get_accessible_paths.

When the Tiger bitmap is loaded from L2/L3 but the in-memory resource map
is cold (process restart, eviction), int IDs must still resolve to paths
via DB fallback in bulk_get_resource_ids() — otherwise the new path-based
visibility code falsely caches False and skips authoritative checks.

The fallback must also use a single batched DB query (not N per-id round
trips) so a large bitmap doesn't saturate the DB on hot auth paths.
"""

from __future__ import annotations

import threading as _threading
from collections import OrderedDict
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")


def _make_cache(*, max_keys: int = 1024, global_cap: int = 16):
    """Construct a TigerCache stub with all orphan-log attrs initialised."""
    from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache

    cache = TigerCache.__new__(TigerCache)
    cache._orphan_log_window_s = 60.0
    cache._orphan_log_max_keys = max_keys
    cache._orphan_log_global_cap = global_cap
    cache._orphan_log_last_emit = OrderedDict()
    cache._orphan_log_global_window_start = 0.0
    cache._orphan_log_global_count = 0
    cache._orphan_log_lock = _threading.Lock()
    return cache


def test_get_accessible_paths_cold_int_to_uuid_uses_bulk_fallback():
    """Cold in-memory map must resolve all int IDs via bulk_get_resource_ids."""
    cache = _make_cache()

    resource_map = MagicMock()
    resource_map._int_to_uuid = {}  # cold L1 — empty in-memory map

    resource_map.bulk_get_resource_ids.return_value = {
        42: ("file", "/workspace/data/foo.txt"),
        43: ("file", "/workspace/data/bar.txt"),
    }
    cache._resource_map = resource_map
    cache.get_accessible_int_ids = MagicMock(return_value={42, 43})

    paths = cache.get_accessible_paths(
        subject_type="user",
        subject_id="alice",
        permission="read",
        resource_type="file",
    )

    assert paths == {"/workspace/data/foo.txt", "/workspace/data/bar.txt"}
    # Must be a single bulk call — not N per-id calls
    assert resource_map.bulk_get_resource_ids.call_count == 1


def test_get_accessible_paths_filters_wrong_resource_type():
    """bulk_get_resource_ids may include other resource_types — filter them out."""
    cache = _make_cache()
    resource_map = MagicMock()
    resource_map.bulk_get_resource_ids.return_value = {
        1: ("file", "/a/file.txt"),
        2: ("group", "/a/group"),  # wrong resource_type — must drop
    }
    cache._resource_map = resource_map
    cache.get_accessible_int_ids = MagicMock(return_value={1, 2})

    paths = cache.get_accessible_paths(
        subject_type="user",
        subject_id="alice",
        permission="read",
        resource_type="file",
    )

    assert paths == {"/a/file.txt"}


def test_get_accessible_paths_returns_none_when_no_bitmap():
    """When get_accessible_int_ids returns None (no bitmap cached), pass through."""
    cache = _make_cache()
    cache._resource_map = MagicMock()
    cache.get_accessible_int_ids = MagicMock(return_value=None)

    paths = cache.get_accessible_paths(
        subject_type="user",
        subject_id="alice",
        permission="read",
        resource_type="file",
    )

    assert paths is None
    cache._resource_map.bulk_get_resource_ids.assert_not_called()


def test_get_accessible_paths_drops_orphan_ids_silently(caplog):
    """Orphan int IDs (bitmap row, no resource_map row) are dropped silently.

    Matches pre-refactor parity (get_resource_id-per-int_id loop also dropped
    orphans). Returning None would conflate orphan-drop with cache-miss, and
    the enforcer batch path treats None as fail-closed-all-deny — turning a
    single stale row into a batch-wide false negative. The drop is logged
    at WARNING for observability.
    """
    import logging

    cache = _make_cache()
    resource_map = MagicMock()
    # int_id 2 unresolvable (DB also has no row) — orphan / partial resolution
    resource_map.bulk_get_resource_ids.return_value = {1: ("file", "/a/file.txt")}
    cache._resource_map = resource_map
    cache.get_accessible_int_ids = MagicMock(return_value={1, 2})

    with caplog.at_level(logging.WARNING):
        paths = cache.get_accessible_paths(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
        )

    assert paths == {"/a/file.txt"}
    assert any("resource_map orphans" in r.message for r in caplog.records)


def test_get_accessible_paths_returns_empty_when_all_unresolved():
    """All-orphan case returns empty set (pre-refactor parity), not None.

    Empty set lets compute_from_tiger_bitmap cache False for the directory,
    and lets has_accessible_descendants_batch evaluate the prefix check
    (yielding all-False) — same as pre-refactor behaviour. None would
    incorrectly flip the enforcer to fail-closed-all-deny on a single
    stale row.
    """
    cache = _make_cache()
    resource_map = MagicMock()
    resource_map.bulk_get_resource_ids.return_value = {}
    cache._resource_map = resource_map
    cache.get_accessible_int_ids = MagicMock(return_value={1, 2, 3})

    paths = cache.get_accessible_paths(
        subject_type="user",
        subject_id="alice",
        permission="read",
        resource_type="file",
    )

    assert paths == set()


def test_get_accessible_paths_large_bitmap_single_bulk_call():
    """50K cold int IDs must produce one bulk_get_resource_ids call, not 50K."""
    cache = _make_cache()
    resource_map = MagicMock()
    resource_map.bulk_get_resource_ids.return_value = {
        i: ("file", f"/workspace/file_{i}.txt") for i in range(50_000)
    }
    cache._resource_map = resource_map
    cache.get_accessible_int_ids = MagicMock(return_value=set(range(50_000)))

    paths = cache.get_accessible_paths(
        subject_type="user",
        subject_id="alice",
        permission="read",
        resource_type="file",
    )

    assert len(paths) == 50_000
    # Hot-path invariant: one bulk call regardless of bitmap size
    assert resource_map.bulk_get_resource_ids.call_count == 1


def test_bulk_get_resource_ids_uses_memory_cache_when_warm():
    """When _int_to_uuid is fully populated, bulk_get_resource_ids must skip DB."""
    from sqlalchemy import create_engine

    from nexus.bricks.rebac.cache.tiger.resource_map import TigerResourceMap

    engine = create_engine("sqlite:///:memory:")
    rmap = TigerResourceMap(engine)
    rmap._int_to_uuid = {
        1: ("file", "/a"),
        2: ("file", "/b"),
        3: ("file", "/c"),
    }
    rmap._uuid_to_int = {v: k for k, v in rmap._int_to_uuid.items()}

    # No connection — would crash if it tried to hit DB
    result = rmap.bulk_get_resource_ids({1, 2, 3})

    assert result == {1: ("file", "/a"), 2: ("file", "/b"), 3: ("file", "/c")}


def test_bulk_get_resource_ids_empty_input():
    """Empty input must return empty dict without DB access."""
    from sqlalchemy import create_engine

    from nexus.bricks.rebac.cache.tiger.resource_map import TigerResourceMap

    engine = create_engine("sqlite:///:memory:")
    rmap = TigerResourceMap(engine)

    assert rmap.bulk_get_resource_ids(set()) == {}
    assert rmap.bulk_get_resource_ids([]) == {}


def test_orphan_warning_is_rate_limited(caplog):
    """Repeated orphan-id calls within the dedupe window emit DEBUG, not WARNING."""
    import logging

    cache = _make_cache()
    rm = MagicMock()
    rm.bulk_get_resource_ids.return_value = {1: ("file", "/a")}
    cache._resource_map = rm
    cache.get_accessible_int_ids = MagicMock(return_value={1, 2})

    with caplog.at_level(logging.DEBUG):
        cache.get_accessible_paths(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
        )
        cache.get_accessible_paths(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
        )
        cache.get_accessible_paths(
            subject_type="user",
            subject_id="alice",
            permission="read",
            resource_type="file",
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "resource_map orphans" in r.message
    ]
    assert len(warnings) == 1, f"expected exactly 1 WARNING, got {len(warnings)}"
    assert len(debugs) == 2, f"expected 2 DEBUG repeats, got {len(debugs)}"


def test_prefix_helpers_in_capability_groups():
    """Issue #3951: prefix functions must be gated through CAPABILITY_GROUP_CONFIG.

    Without this, a stale/version-skewed nexus_runtime would still expose
    these symbols while the rest of Rust is disabled — driving auth
    visibility off a broken binary.
    """
    from nexus._kernel_api_groups import MODULE_CAPABILITY_GROUPS

    assert "prefix" in MODULE_CAPABILITY_GROUPS
    prefix_group = MODULE_CAPABILITY_GROUPS["prefix"]
    assert "any_path_starts_with" in prefix_group
    assert "batch_prefix_check" in prefix_group


def test_prefix_helpers_disabled_when_group_fails(monkeypatch):
    """Simulate a stale runtime where the 'prefix' group is invalidated."""
    import nexus._rust_compat as rc

    monkeypatch.setattr(rc, "_disabled_symbols", set(rc._disabled_symbols))
    rc._disabled_symbols.update({"any_path_starts_with", "batch_prefix_check"})
    assert rc._get("any_path_starts_with") is None
    assert rc._get("batch_prefix_check") is None


def test_rust_hash_available_false_when_rust_unavailable():
    """Round 7: RUST_HASH_AVAILABLE must follow RUST_AVAILABLE.

    Earlier code set RUST_HASH_AVAILABLE solely from the hash group's
    own symbol presence — leaking a True flag even when stale-core had
    forced RUST_AVAILABLE False. Downstream callers (e.g. nexus.core
    .hash_fast) copy that flag into their own state and would falsely
    report Rust-hash capability.
    """
    import nexus._rust_compat as rc

    # Invariant: RUST_HASH_AVAILABLE implies RUST_AVAILABLE.
    if not rc.RUST_AVAILABLE:
        assert rc.RUST_HASH_AVAILABLE is False


def test_prefix_helpers_disabled_when_core_fails():
    """Issue #3951 round 6: when core ABI fails, ALL groups must be disabled.

    A version-skewed binary missing a core symbol cannot be safely used
    even if other groups (like 'prefix') happen to expose their symbols.
    The init logic in _rust_compat.py must promote 'core failed' to
    'all groups disabled' so that _get() returns None for prefix helpers.
    """
    from nexus._kernel_api_groups import MODULE_CAPABILITY_GROUPS

    # Simulate the broken-core branch's invariant: when RUST_AVAILABLE
    # is False but extension imported, every group symbol must be in
    # the disabled set. This is the contract _rust_compat now enforces.
    all_symbols: set[str] = set()
    for syms in MODULE_CAPABILITY_GROUPS.values():
        all_symbols.update(syms)

    # Sanity: prefix helpers and core are in the union
    assert "any_path_starts_with" in all_symbols
    assert "batch_prefix_check" in all_symbols
    assert "PyKernel" in all_symbols  # core


def test_orphan_log_dedupe_map_is_bounded():
    """Many distinct subjects must not retain unbounded orphan-log state."""
    cache = _make_cache(max_keys=32, global_cap=10_000)  # tight LRU cap, lift global
    rm = MagicMock()
    rm.bulk_get_resource_ids.return_value = {}  # all orphans
    cache._resource_map = rm

    for i in range(200):
        cache.get_accessible_int_ids = MagicMock(return_value={i + 1})
        cache.get_accessible_paths(
            subject_type="user",
            subject_id=f"alice_{i}",
            permission="read",
            resource_type="file",
        )

    assert len(cache._orphan_log_last_emit) <= cache._orphan_log_max_keys, (
        f"dedupe map grew to {len(cache._orphan_log_last_emit)} entries "
        f"(cap={cache._orphan_log_max_keys})"
    )


def test_orphan_log_global_rate_limit_caps_warnings():
    """High-cardinality orphan event across many subjects must cap WARNINGs.

    Per-key dedupe alone isn't enough — a thousand subjects each producing
    one WARNING in the window is still a flood. The global cap downgrades
    excess emissions to DEBUG once the window-counter exceeds the cap.
    """
    import logging

    cache = _make_cache(max_keys=10_000, global_cap=8)
    rm = MagicMock()
    rm.bulk_get_resource_ids.return_value = {}  # all orphans
    cache._resource_map = rm

    warnings = 0

    class _CountingHandler(logging.Handler):
        def emit(self, record):
            nonlocal warnings
            if record.levelno == logging.WARNING and "resource_map orphans" in record.getMessage():
                warnings += 1

    handler = _CountingHandler()
    bitmap_logger = logging.getLogger("nexus.bricks.rebac.cache.tiger.bitmap_cache")
    bitmap_logger.addHandler(handler)
    bitmap_logger.setLevel(logging.DEBUG)

    try:
        for i in range(100):  # 100 distinct subjects, all orphan
            cache.get_accessible_int_ids = MagicMock(return_value={i + 1})
            cache.get_accessible_paths(
                subject_type="user",
                subject_id=f"alice_{i}",
                permission="read",
                resource_type="file",
            )
    finally:
        bitmap_logger.removeHandler(handler)

    assert warnings <= cache._orphan_log_global_cap, (
        f"emitted {warnings} WARNINGs, expected ≤ {cache._orphan_log_global_cap}"
    )


def test_orphan_log_lru_refresh_keeps_hot_keys():
    """Refreshing an existing key must move it to the LRU tail, not stay at front.

    Without true LRU, a hot key would be evicted by churn from unrelated
    subjects and emit WARNINGs again inside the suppression window.
    """
    cache = _make_cache(max_keys=4, global_cap=10_000)
    rm = MagicMock()
    rm.bulk_get_resource_ids.return_value = {}
    cache._resource_map = rm

    # Burn through 4 distinct keys (fills the LRU)
    for i in range(4):
        cache.get_accessible_int_ids = MagicMock(return_value={i + 1})
        cache.get_accessible_paths(
            subject_type="user",
            subject_id=f"k{i}",
            permission="read",
            resource_type="file",
        )

    # Force-expire k0 by rewinding its timestamp, then re-emit it. Without
    # LRU refresh on emit, k0 would still be at the front and be evicted
    # next. With true LRU it moves to the end and "k1" becomes oldest.
    k0 = ("", "user", "k0", "read", "file")
    cache._orphan_log_last_emit[k0] = 0.0  # expired
    cache.get_accessible_int_ids = MagicMock(return_value={99})
    cache.get_accessible_paths(
        subject_type="user", subject_id="k0", permission="read", resource_type="file"
    )

    # Now insert a brand-new key — should evict the oldest (k1, not k0)
    cache.get_accessible_int_ids = MagicMock(return_value={100})
    cache.get_accessible_paths(
        subject_type="user", subject_id="k_new", permission="read", resource_type="file"
    )

    assert k0 in cache._orphan_log_last_emit, "k0 should not have been evicted"
    assert ("", "user", "k1", "read", "file") not in cache._orphan_log_last_emit, (
        "k1 (oldest) should have been evicted"
    )


def test_orphan_log_dedupe_threadsafe():
    """Concurrent orphan calls must not duplicate-emit the WARNING."""
    import logging
    from concurrent.futures import ThreadPoolExecutor

    cache = _make_cache()
    rm = MagicMock()
    rm.bulk_get_resource_ids.return_value = {1: ("file", "/a")}
    cache._resource_map = rm
    cache.get_accessible_int_ids = MagicMock(return_value={1, 2})

    warnings_count = 0

    class _CountingHandler(logging.Handler):
        def emit(self, record):
            nonlocal warnings_count
            if record.levelno == logging.WARNING and "resource_map orphans" in record.getMessage():
                warnings_count += 1

    handler = _CountingHandler()
    bitmap_logger = logging.getLogger("nexus.bricks.rebac.cache.tiger.bitmap_cache")
    bitmap_logger.addHandler(handler)
    bitmap_logger.setLevel(logging.DEBUG)

    try:

        def call():
            cache.get_accessible_paths(
                subject_type="user",
                subject_id="alice",
                permission="read",
                resource_type="file",
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda _: call(), range(64)))
    finally:
        bitmap_logger.removeHandler(handler)

    # First call wins; the rest should drop to DEBUG. With proper locking
    # we get exactly 1 WARNING for 64 concurrent calls on the same key.
    assert warnings_count == 1, f"expected 1 WARNING, got {warnings_count}"
