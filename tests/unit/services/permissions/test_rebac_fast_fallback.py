"""Tests for optional Rust acceleration fallback in ReBAC helpers."""

from __future__ import annotations

import importlib
import sys
import types
from typing import cast
from unittest.mock import patch


def test_rebac_fast_imports_without_nexus_runtime_and_uses_python_fallback() -> None:
    """Absent Rust runtime should disable acceleration without breaking imports."""
    module_names = (
        "nexus_runtime",
        "nexus._rust_compat",
        "nexus.bricks.rebac.utils.fast",
    )
    saved = {name: sys.modules.get(name) for name in module_names}
    try:
        for name in module_names:
            sys.modules.pop(name, None)

        with patch.dict(sys.modules, {"nexus_runtime": cast(types.ModuleType, None)}):
            fast = importlib.import_module("nexus.bricks.rebac.utils.fast")

            assert fast.is_rust_available() is False
            result = fast.check_permissions_bulk_with_fallback(
                [(("user", "alice"), "read", ("file", "/doc.txt"))],
                [
                    {
                        "subject_type": "user",
                        "subject_id": "alice",
                        "subject_relation": None,
                        "relation": "read",
                        "object_type": "file",
                        "object_id": "/doc.txt",
                    }
                ],
                {},
            )
            assert result[("user", "alice", "read", "file", "/doc.txt")] is True
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


def test_python_fallback_does_not_treat_userset_subject_as_direct_grant() -> None:
    """group:eng#member grants must not grant group:eng itself."""
    from nexus.bricks.rebac.utils import fast

    result = fast.check_permissions_bulk_with_fallback(
        [(("group", "eng"), "read", ("file", "/doc.txt"))],
        [
            {
                "subject_type": "group",
                "subject_id": "eng",
                "subject_relation": "member",
                "relation": "read",
                "object_type": "file",
                "object_id": "/doc.txt",
            }
        ],
        {},
        force_python=True,
    )

    assert result[("group", "eng", "read", "file", "/doc.txt")] is False


def test_python_fallback_denies_conditioned_tuple_without_context() -> None:
    """Bulk fast fallback has no ABAC context, so conditioned tuples fail closed."""
    from nexus.bricks.rebac.utils import fast

    result = fast.check_permissions_bulk_with_fallback(
        [(("user", "alice"), "read", ("file", "/doc.txt"))],
        [
            {
                "subject_type": "user",
                "subject_id": "alice",
                "subject_relation": None,
                "relation": "read",
                "object_type": "file",
                "object_id": "/doc.txt",
                "conditions": {"allowed_ips": ["10.0.0.0/8"]},
            }
        ],
        {},
        force_python=True,
    )

    assert result[("user", "alice", "read", "file", "/doc.txt")] is False


# ---------------------------------------------------------------------------
# Issue #4240: Rust-unavailable should not log a per-call WARNING
# ---------------------------------------------------------------------------


def test_no_warning_emitted_when_rust_unavailable_on_single_check(caplog) -> None:
    """``ReBACManager._compute_permission_with_limits`` must skip its Rust
    try/except entirely when ``is_rust_available()`` is False — otherwise
    every permission decision raises + catches ``RuntimeError`` and logs
    ``warning("Rust single permission check failed, falling back to
    Python: ...")``, which spams ~5 lines per /api/v2/search/query and
    gives operators a misleading remediation hint
    (``cargo build -p nexus-cluster`` does NOT install a Python extension).

    Issue #4240.
    """
    import logging

    from nexus.bricks.rebac.utils import fast

    # Test is meaningful only when Rust is absent in the environment.
    if fast.is_rust_available():
        import pytest as _pytest

        _pytest.skip("Rust extension present in this environment; test N/A")

    caplog.set_level(logging.WARNING, logger="nexus.bricks.rebac.manager")
    caplog.set_level(logging.WARNING, logger="nexus.bricks.rebac.utils.fast")

    # Exercise the real manager path. A minimal stub keeps this test
    # hermetic (no DB / no full bootstrap): we monkey-construct just
    # enough of ReBACManager to call _compute_permission_with_limits.
    from unittest.mock import MagicMock

    from nexus.bricks.rebac.domain import Entity
    from nexus.bricks.rebac.manager import ReBACManager
    from nexus.contracts.rebac_types import TraversalStats

    mgr = MagicMock(spec=ReBACManager)
    mgr._fetch_tuples_for_rust = MagicMock(return_value=[])
    mgr._get_namespace_configs_for_rust = MagicMock(return_value={})
    mgr._compute_permission_zone_aware_with_limits = MagicMock(return_value=False)

    # Bind the unbound method so it uses our mock as self.
    ReBACManager._compute_permission_with_limits(
        mgr,
        subject=Entity("user", "alice"),
        permission="read",
        obj=Entity("file", "/doc.txt"),
        zone_id="root",
        stats=TraversalStats(),
    )

    offenders = [
        rec.message
        for rec in caplog.records
        if rec.name == "nexus.bricks.rebac.manager"
        and (
            "falling back to Python" in rec.message
            or "Rust acceleration not available" in rec.message
        )
    ]
    assert offenders == [], (
        f"Rust-unavailable should be silent on the hot path (#4240); got: {offenders}"
    )


# ---------------------------------------------------------------------------
# Issue #4240 (b): Python fallback should be O(N+T), not O(N*T)
# ---------------------------------------------------------------------------


def test_python_fallback_scales_linearly() -> None:
    """100 paths × 1000 tuples must complete in well under a second.

    The pre-optimization implementation scanned the full tuples list
    inside ``_compute_permission_simple`` for every check, so a search
    with 5 results on a few hundred tuples hit ~4500ms (see #4240
    reporter's ``permission_filter_ms``). Post-optimization the per-call
    work is a set lookup, so even a 100x larger problem (100 × 1000)
    should land in tens of milliseconds.

    Threshold is generous (1000ms) to avoid CI flake while still
    catching a regression to quadratic behavior.
    """
    import time

    from nexus.bricks.rebac.utils import fast

    # 1000 unrelated tuples (no grants for our subject).
    noise_tuples = [
        {
            "subject_type": "user",
            "subject_id": f"other_{i}",
            "subject_relation": None,
            "relation": "read",
            "object_type": "file",
            "object_id": f"/noise/{i}.md",
        }
        for i in range(1000)
    ]
    # Plus the grants we actually want: admin reads /target/{i}.md.
    grant_tuples = [
        {
            "subject_type": "user",
            "subject_id": "admin",
            "subject_relation": None,
            "relation": "read",
            "object_type": "file",
            "object_id": f"/target/{i}.md",
        }
        for i in range(100)
    ]
    tuples = noise_tuples + grant_tuples

    checks = [(("user", "admin"), "read", ("file", f"/target/{i}.md")) for i in range(100)]

    start = time.perf_counter()
    results = fast.check_permissions_bulk_with_fallback(
        checks=checks,
        tuples=tuples,
        namespace_configs={},
        force_python=True,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    # All 100 checks must be True.
    assert all(results.values()), f"expected all granted; got {sum(results.values())} / 100"
    # Performance guard — generous to avoid CI flake.
    assert elapsed_ms < 1000, (
        f"Python ReBAC fallback should be O(N+T), not O(N*T); "
        f"100 checks × 1000 tuples took {elapsed_ms:.1f}ms"
    )


def test_python_fallback_userset_excluded_from_direct_index() -> None:
    """Userset-subject tuples (``subject_relation`` set) must not match
    as a direct grant — index construction excludes them, preserving the
    pre-optimization safety property."""
    from nexus.bricks.rebac.utils import fast

    results = fast.check_permissions_bulk_with_fallback(
        [(("group", "eng"), "read", ("file", "/doc.txt"))],
        [
            {
                "subject_type": "group",
                "subject_id": "eng",
                "subject_relation": "member",  # userset, not a direct grant
                "relation": "read",
                "object_type": "file",
                "object_id": "/doc.txt",
            }
        ],
        {},
        force_python=True,
    )
    assert results[("group", "eng", "read", "file", "/doc.txt")] is False


def test_python_fallback_conditioned_tuple_excluded_from_direct_index() -> None:
    """Tuples with conditions must remain fail-closed in the simple
    fallback (no ABAC context available)."""
    from nexus.bricks.rebac.utils import fast

    results = fast.check_permissions_bulk_with_fallback(
        [(("user", "alice"), "read", ("file", "/doc.txt"))],
        [
            {
                "subject_type": "user",
                "subject_id": "alice",
                "subject_relation": None,
                "relation": "read",
                "object_type": "file",
                "object_id": "/doc.txt",
                "conditions": {"allowed_ips": ["10.0.0.0/8"]},
            }
        ],
        {},
        force_python=True,
    )
    assert results[("user", "alice", "read", "file", "/doc.txt")] is False


def test_python_fallback_memo_reuses_answers_across_checks() -> None:
    """A single bulk call asking the same (subject, permission, obj) twice
    must compute once. We can't easily count internal calls without
    monkeypatching, so we assert the wall-clock cost of N duplicate checks
    is barely worse than 1 check on a large tuple set.
    """
    import time

    from nexus.bricks.rebac.utils import fast

    tuples = [
        {
            "subject_type": "user",
            "subject_id": f"x_{i}",
            "subject_relation": None,
            "relation": "read",
            "object_type": "file",
            "object_id": f"/x/{i}.md",
        }
        for i in range(2000)
    ]
    # One real grant.
    tuples.append(
        {
            "subject_type": "user",
            "subject_id": "alice",
            "subject_relation": None,
            "relation": "read",
            "object_type": "file",
            "object_id": "/doc.txt",
        }
    )

    duplicate_checks = [(("user", "alice"), "read", ("file", "/doc.txt")) for _ in range(50)]

    start = time.perf_counter()
    results = fast.check_permissions_bulk_with_fallback(
        duplicate_checks, tuples, {}, force_python=True
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert all(results.values())
    # 50 identical checks should be essentially free post-index.
    assert elapsed_ms < 500, f"50 duplicate checks took {elapsed_ms:.1f}ms"


# ---------------------------------------------------------------------------
# Round 1 review: cyclic-union negative memoization (codex finding HIGH)
# ---------------------------------------------------------------------------


def test_cyclic_union_does_not_poison_negative_memo() -> None:
    """Regression for finding HIGH: ``a=union(b,c)``, ``b=union(a)``, direct
    grant on c. The recursion expands b first (b → a is a cycle, returns
    False locally), then expands c → True, so a=True. The previous code
    memoized b=False under that cyclic-False, so a later check on b alone
    in the same bulk call returned False even though b would resolve True
    via the non-cyclic a→c path.

    Post-fix: cycle-tainted False results do NOT enter the memo. b
    recomputes on a fresh stack and resolves True via memo[a]=True.
    """
    from nexus.bricks.rebac.utils import fast

    # Namespace: file has relations a, b, c. a expands b OR c. b expands a.
    namespace_configs = {
        "file": {
            "relations": {
                "a": {"union": ["b", "c"]},
                "b": {"union": ["a"]},
                "c": "direct",
            },
            "permissions": {},
        }
    }

    tuples = [
        {
            "subject_type": "user",
            "subject_id": "alice",
            "subject_relation": None,
            "relation": "c",  # direct grant on c
            "object_type": "file",
            "object_id": "/doc.txt",
        }
    ]

    # Single bulk call with both checks. a must resolve True via c, AND b
    # must also resolve True via a→c — the bulk-order must not differ from
    # the standalone-check answer.
    checks = [
        (("user", "alice"), "a", ("file", "/doc.txt")),
        (("user", "alice"), "b", ("file", "/doc.txt")),
    ]
    results = fast.check_permissions_bulk_with_fallback(
        checks, tuples, namespace_configs, force_python=True
    )

    assert results[("user", "alice", "a", "file", "/doc.txt")] is True
    assert results[("user", "alice", "b", "file", "/doc.txt")] is True, (
        "b must resolve True via a→c expansion — previous code returned False "
        "because a cycle-tainted False on b got memoized when a was computed "
        "first (codex round-1 finding HIGH)."
    )

    # Order-independence check: same fixture, b first.
    checks_reversed = list(reversed(checks))
    results_rev = fast.check_permissions_bulk_with_fallback(
        checks_reversed, tuples, namespace_configs, force_python=True
    )
    assert results_rev == results, "bulk results must be order-independent"


def test_cyclic_relation_with_no_grant_returns_false_consistently() -> None:
    """Companion to the above: when neither cycle path has a direct grant,
    BOTH checks return False, and the False is reproducible on standalone
    re-check (we don't accidentally memoize a wrong True either)."""
    from nexus.bricks.rebac.utils import fast

    namespace_configs = {
        "file": {
            "relations": {
                "a": {"union": ["b"]},
                "b": {"union": ["a"]},
            },
            "permissions": {},
        }
    }
    tuples: list = []
    checks = [
        (("user", "alice"), "a", ("file", "/doc.txt")),
        (("user", "alice"), "b", ("file", "/doc.txt")),
    ]
    results = fast.check_permissions_bulk_with_fallback(
        checks, tuples, namespace_configs, force_python=True
    )
    assert results[("user", "alice", "a", "file", "/doc.txt")] is False
    assert results[("user", "alice", "b", "file", "/doc.txt")] is False


# ---------------------------------------------------------------------------
# Round-2 review (codex finding HIGH): dict-form permission union
# ---------------------------------------------------------------------------


def test_python_fallback_unwraps_dict_form_permission_union() -> None:
    """Regression for codex round-2 HIGH: ``"permissions": {"read":
    {"union": ["viewer"]}}`` is a documented namespace shape. The
    previous fallback iterated the dict directly, yielding the key
    ``"union"`` as a relation name (which doesn't exist) and silently
    denying a valid direct-viewer grant.

    Post-fix: ``_unwrap_userset`` extracts the union member list.
    """
    from nexus.bricks.rebac.utils import fast

    namespace_configs = {
        "file": {
            "relations": {"viewer": "direct"},
            # Permission defined as dict-wrapped union — the bug case.
            "permissions": {"read": {"union": ["viewer"]}},
        }
    }
    tuples = [
        {
            "subject_type": "user",
            "subject_id": "alice",
            "subject_relation": None,
            "relation": "viewer",
            "object_type": "file",
            "object_id": "/doc.txt",
        }
    ]
    results = fast.check_permissions_bulk_with_fallback(
        [(("user", "alice"), "read", ("file", "/doc.txt"))],
        tuples,
        namespace_configs,
        force_python=True,
    )
    assert results[("user", "alice", "read", "file", "/doc.txt")] is True, (
        "viewer grant must expand through {'union': ['viewer']} "
        "permission definition (codex round-2 HIGH)"
    )


def test_python_fallback_handles_list_form_permission() -> None:
    """The list-form ``"read": ["viewer"]`` must keep working — the
    round-2 helper normalizes both shapes."""
    from nexus.bricks.rebac.utils import fast

    namespace_configs = {
        "file": {
            "relations": {"viewer": "direct"},
            "permissions": {"read": ["viewer"]},
        }
    }
    tuples = [
        {
            "subject_type": "user",
            "subject_id": "alice",
            "subject_relation": None,
            "relation": "viewer",
            "object_type": "file",
            "object_id": "/doc.txt",
        }
    ]
    results = fast.check_permissions_bulk_with_fallback(
        [(("user", "alice"), "read", ("file", "/doc.txt"))],
        tuples,
        namespace_configs,
        force_python=True,
    )
    assert results[("user", "alice", "read", "file", "/doc.txt")] is True
