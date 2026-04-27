"""Tests for optional Rust acceleration fallback in ReBAC helpers."""

import builtins
import importlib
import sys
from types import ModuleType
from typing import Any


def test_fast_module_imports_without_nexus_kernel(monkeypatch) -> None:
    """Missing nexus_kernel should disable Rust, not break imports."""
    old_fast = sys.modules.pop("nexus.bricks.rebac.utils.fast", None)
    old_kernel = sys.modules.pop("nexus_kernel", None)
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> ModuleType:
        if name == "nexus_kernel":
            raise ModuleNotFoundError("No module named 'nexus_kernel'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    try:
        fast = importlib.import_module("nexus.bricks.rebac.utils.fast")

        assert fast.RUST_AVAILABLE is False
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
        sys.modules.pop("nexus.bricks.rebac.utils.fast", None)
        if old_fast is not None:
            sys.modules["nexus.bricks.rebac.utils.fast"] = old_fast
        if old_kernel is not None:
            sys.modules["nexus_kernel"] = old_kernel


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
