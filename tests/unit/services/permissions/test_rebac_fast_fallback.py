"""Tests for optional Rust acceleration fallback in ReBAC helpers."""


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
