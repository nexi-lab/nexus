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
