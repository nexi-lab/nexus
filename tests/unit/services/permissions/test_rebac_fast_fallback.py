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
# Cycle handling: bulk_evaluator is now the primary path. The round-1
# cyclic-union test was specific to the old _compute_permission_simple
# expansion (which is now only a degraded backstop when bulk_evaluator
# can't be imported). bulk_evaluator's cycle semantics are tracked
# upstream — see graph/bulk_evaluator.py — and any divergence there is a
# pre-existing bug, not a regression from this PR.
# ---------------------------------------------------------------------------


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
            "relations": {"viewer": {}},  # leaf direct-grant (canonical shape)
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
            "relations": {"viewer": {}},  # leaf direct-grant (canonical shape)
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


# ---------------------------------------------------------------------------
# Round-3 review (codex HIGH): tupleToUserset inheritance for wildcard fix
# ---------------------------------------------------------------------------


def test_python_fallback_grants_via_tupleToUserset_parent_inheritance() -> None:
    """The #4239 wildcard fix normalizes ``/workspaces/ws1/**`` to
    ``/workspaces/ws1``. The default file namespace inherits viewer
    access to descendants via ``parent_viewer``, a tupleToUserset
    relation. Without bulk_evaluator the previous simplified fallback
    returned False for descendant files in Rust-free edge images,
    silently breaking the round-2 wildcard advertisement.

    Codex round-3 HIGH: this test exercises the production
    ``check_permissions_bulk_with_fallback`` path (force_python=True)
    with a direct_viewer tuple on the directory plus an explicit
    ``parent`` tuple linking the file to its directory — the shape
    bulk_evaluator + the canonical file namespace expect.
    """
    from nexus.bricks.rebac.utils import fast

    # Mirror the production file namespace (default_namespaces.py).
    namespace_configs = {
        "file": {
            "relations": {
                "parent": {},
                "direct_viewer": {},
                "parent_viewer": {
                    "tupleToUserset": {
                        "tupleset": "parent",
                        "computedUserset": "viewer",
                    }
                },
                "viewer": {"union": ["direct_viewer", "parent_viewer"]},
            },
            "permissions": {"read": ["viewer"]},
        }
    }
    tuples = [
        # admin gets direct_viewer on the directory. No explicit
        # ``parent`` tuple — round-7 review (codex HIGH): the bulk
        # fallback must synthesize parent links from the path itself,
        # otherwise the #4239 wildcard fix is dead in Rust-free edge
        # images.
        {
            "subject_type": "user",
            "subject_id": "admin",
            "subject_relation": None,
            "relation": "direct_viewer",
            "object_type": "file",
            "object_id": "/workspaces/ws1",
        },
    ]
    results = fast.check_permissions_bulk_with_fallback(
        [(("user", "admin"), "read", ("file", "/workspaces/ws1/a.md"))],
        tuples,
        namespace_configs,
        force_python=True,
    )
    assert results[("user", "admin", "read", "file", "/workspaces/ws1/a.md")] is True, (
        "tupleToUserset parent inheritance must work in the Rust-free fallback — "
        "otherwise the round-2 wildcard fix (#4239) is dead in edge images "
        "(codex round-3 HIGH)."
    )


def test_python_fallback_deep_directory_grant_inheritance() -> None:
    """Round-7 review (codex HIGH): a grant on /a inherits to
    /a/b/c/d/file.md via the synthesized parent chain. No explicit
    ``parent`` tuples needed.
    """
    from nexus.bricks.rebac.utils import fast

    namespace_configs = {
        "file": {
            "relations": {
                "parent": {},
                "direct_viewer": {},
                "parent_viewer": {
                    "tupleToUserset": {
                        "tupleset": "parent",
                        "computedUserset": "viewer",
                    }
                },
                "viewer": {"union": ["direct_viewer", "parent_viewer"]},
            },
            "permissions": {"read": ["viewer"]},
        }
    }
    # Grant on /a — must reach /a/b/c/d/file.md without any explicit
    # parent tuples.
    tuples = [
        {
            "subject_type": "user",
            "subject_id": "admin",
            "subject_relation": None,
            "relation": "direct_viewer",
            "object_type": "file",
            "object_id": "/a",
        },
    ]
    results = fast.check_permissions_bulk_with_fallback(
        [(("user", "admin"), "read", ("file", "/a/b/c/d/file.md"))],
        tuples,
        namespace_configs,
        force_python=True,
    )
    assert results[("user", "admin", "read", "file", "/a/b/c/d/file.md")] is True, (
        "4-level deep grant must inherit via synthesized parent chain (codex round-7 HIGH)"
    )


# ---------------------------------------------------------------------------
# Round-4 review (codex CRITICAL/HIGH): bulk_evaluator semantic correctness
# ---------------------------------------------------------------------------


def test_python_fallback_intersection_requires_all() -> None:
    """Codex round-4 CRITICAL: permission-level intersection must be
    AND — granted only if every userset is granted. The pre-fix
    bulk_evaluator flattened ``{"intersection": [...]}`` into a list
    and applied OR semantics, so a single matching userset would grant
    even when other required usersets denied.
    """
    from nexus.bricks.rebac.utils import fast

    namespace_configs = {
        "file": {
            "relations": {"viewer": {}, "mfa": {}},
            "permissions": {"read": {"intersection": ["viewer", "mfa"]}},
        }
    }
    # alice has viewer but NOT mfa — must NOT pass intersection.
    tuples = [
        {
            "subject_type": "user",
            "subject_id": "alice",
            "subject_relation": None,
            "relation": "viewer",
            "object_type": "file",
            "object_id": "/doc.txt",
        },
    ]
    results = fast.check_permissions_bulk_with_fallback(
        [(("user", "alice"), "read", ("file", "/doc.txt"))],
        tuples,
        namespace_configs,
        force_python=True,
    )
    assert results[("user", "alice", "read", "file", "/doc.txt")] is False, (
        "intersection AND must require BOTH viewer + mfa; viewer-only "
        "must NOT grant read (codex round-4 CRITICAL)"
    )

    # Now grant both — should grant.
    tuples.append(
        {
            "subject_type": "user",
            "subject_id": "alice",
            "subject_relation": None,
            "relation": "mfa",
            "object_type": "file",
            "object_id": "/doc.txt",
        }
    )
    results2 = fast.check_permissions_bulk_with_fallback(
        [(("user", "alice"), "read", ("file", "/doc.txt"))],
        tuples,
        namespace_configs,
        force_python=True,
    )
    assert results2[("user", "alice", "read", "file", "/doc.txt")] is True


def test_python_fallback_exclusion_is_not() -> None:
    """Codex round-4 CRITICAL: permission-level exclusion must be NOT —
    granted only when the excluded relation is NOT held."""
    from nexus.bricks.rebac.utils import fast

    namespace_configs = {
        "file": {
            "relations": {"denied": {}},
            "permissions": {"read": {"exclusion": "denied"}},
        }
    }
    # alice is on the deny list — must NOT pass.
    tuples = [
        {
            "subject_type": "user",
            "subject_id": "alice",
            "subject_relation": None,
            "relation": "denied",
            "object_type": "file",
            "object_id": "/doc.txt",
        },
    ]
    results = fast.check_permissions_bulk_with_fallback(
        [
            (("user", "alice"), "read", ("file", "/doc.txt")),
            (("user", "bob"), "read", ("file", "/doc.txt")),
        ],
        tuples,
        namespace_configs,
        force_python=True,
    )
    assert results[("user", "alice", "read", "file", "/doc.txt")] is False, (
        "exclusion: alice on deny list must NOT grant (codex round-4 CRITICAL)"
    )
    assert results[("user", "bob", "read", "file", "/doc.txt")] is True, (
        "exclusion: bob not on deny list must grant"
    )


def test_python_fallback_unknown_permission_operator_fails_closed() -> None:
    """Codex round-4 CRITICAL: a permission defined with an unknown
    dict operator (e.g. operator typo) must fail closed, not silently
    fall through to a flattened-OR behavior."""
    from nexus.bricks.rebac.utils import fast

    namespace_configs = {
        "file": {
            "relations": {"viewer": {}},
            "permissions": {"read": {"unknown_op": ["viewer"]}},
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
        },
    ]
    results = fast.check_permissions_bulk_with_fallback(
        [(("user", "alice"), "read", ("file", "/doc.txt"))],
        tuples,
        namespace_configs,
        force_python=True,
    )
    assert results[("user", "alice", "read", "file", "/doc.txt")] is False


def test_python_fallback_empty_intersection_fails_closed() -> None:
    """Round-5 review (codex HIGH): ``{"intersection": []}`` previously
    short-circuited True (vacuous all-of nothing). Must fail closed —
    a generated or partially-migrated namespace with an empty operand
    would otherwise grant every subject."""
    from nexus.bricks.rebac.utils import fast

    namespace_configs = {
        "file": {
            "relations": {"viewer": {}},
            "permissions": {"read": {"intersection": []}},
        }
    }
    results = fast.check_permissions_bulk_with_fallback(
        [(("user", "alice"), "read", ("file", "/doc.txt"))],
        [],
        namespace_configs,
        force_python=True,
    )
    assert results[("user", "alice", "read", "file", "/doc.txt")] is False


def test_python_fallback_empty_union_fails_closed() -> None:
    """Round-5 review: empty union list also fails closed."""
    from nexus.bricks.rebac.utils import fast

    namespace_configs = {
        "file": {
            "relations": {"viewer": {}},
            "permissions": {"read": {"union": []}},
        }
    }
    results = fast.check_permissions_bulk_with_fallback(
        [(("user", "alice"), "read", ("file", "/doc.txt"))],
        [],
        namespace_configs,
        force_python=True,
    )
    assert results[("user", "alice", "read", "file", "/doc.txt")] is False


def test_python_fallback_empty_relation_intersection_fails_closed() -> None:
    """Round-6 review (codex HIGH): relation-level intersection with
    an empty operand list previously short-circuited True after zero
    iterations and granted every subject via any permission mapped
    through that relation. Must fail closed."""
    from nexus.bricks.rebac.utils import fast

    namespace_configs = {
        "file": {
            "relations": {
                # Relation-level intersection of nothing — must NOT grant.
                "everyone": {"intersection": []},
            },
            "permissions": {"read": ["everyone"]},
        }
    }
    results = fast.check_permissions_bulk_with_fallback(
        [(("user", "alice"), "read", ("file", "/doc.txt"))],
        [],
        namespace_configs,
        force_python=True,
    )
    assert results[("user", "alice", "read", "file", "/doc.txt")] is False


def test_python_fallback_empty_relation_union_fails_closed() -> None:
    """Round-6 review: relation-level empty union also fails closed."""
    from nexus.bricks.rebac.utils import fast

    namespace_configs = {
        "file": {
            "relations": {"viewer": {"union": []}},
            "permissions": {"read": ["viewer"]},
        }
    }
    results = fast.check_permissions_bulk_with_fallback(
        [(("user", "alice"), "read", ("file", "/doc.txt"))],
        [],
        namespace_configs,
        force_python=True,
    )
    assert results[("user", "alice", "read", "file", "/doc.txt")] is False


def test_python_fallback_empty_exclusion_fails_closed() -> None:
    """Round-5 review (codex HIGH): ``{"exclusion": ""}`` previously
    granted because ``not _recurse(..., "")`` evaluated False for the
    unknown empty relation and the NOT inverted it. Must fail closed."""
    from nexus.bricks.rebac.utils import fast

    namespace_configs = {
        "file": {
            "relations": {"denied": {}},
            "permissions": {"read": {"exclusion": ""}},
        }
    }
    results = fast.check_permissions_bulk_with_fallback(
        [(("user", "alice"), "read", ("file", "/doc.txt"))],
        [],
        namespace_configs,
        force_python=True,
    )
    assert results[("user", "alice", "read", "file", "/doc.txt")] is False


def test_python_fallback_cyclic_union_order_independence() -> None:
    """Codex round-4 HIGH (restored from round-1): in a bulk call with
    a cyclic union plus a real grant via another path, the bulk
    ordering must NOT change the result. The pre-round-4 bulk_evaluator
    memoized cycle-tainted False, making ``[a, b]`` deny ``b`` even
    though ``b`` alone resolves True via the a→c path.
    """
    from nexus.bricks.rebac.utils import fast

    namespace_configs = {
        "file": {
            "relations": {
                "a": {"union": ["b", "c"]},
                "b": {"union": ["a"]},
                "c": {},
            },
            "permissions": {"read": ["a", "b"]},
        }
    }
    tuples = [
        {
            "subject_type": "user",
            "subject_id": "alice",
            "subject_relation": None,
            "relation": "c",
            "object_type": "file",
            "object_id": "/doc.txt",
        },
    ]
    forward = fast.check_permissions_bulk_with_fallback(
        [
            (("user", "alice"), "a", ("file", "/doc.txt")),
            (("user", "alice"), "b", ("file", "/doc.txt")),
        ],
        tuples,
        namespace_configs,
        force_python=True,
    )
    reverse = fast.check_permissions_bulk_with_fallback(
        [
            (("user", "alice"), "b", ("file", "/doc.txt")),
            (("user", "alice"), "a", ("file", "/doc.txt")),
        ],
        tuples,
        namespace_configs,
        force_python=True,
    )
    assert forward[("user", "alice", "a", "file", "/doc.txt")] is True
    assert forward[("user", "alice", "b", "file", "/doc.txt")] is True, (
        "b must resolve True via a→c expansion in either bulk order — "
        "cycle-tainted False must NOT be memoized (codex round-4 HIGH)"
    )
    assert forward == reverse, "bulk results must be order-independent"
