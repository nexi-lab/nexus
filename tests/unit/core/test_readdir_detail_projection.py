"""sys_readdir(details=True) returns the full FileMetadata projection.

Service callers migrating off the ``metastore_list_paginated`` kernel
primitive consume ``sys_readdir(details=True)`` instead — so the detail
dict must carry every ``FileMetadata`` field, not just the display subset.
"""

from __future__ import annotations

from tests.testkit.nexus_factory import make_test_nexus

# Every field on contracts.metadata.FileMetadata — the detail dict is a
# 1:1 projection so callers never need to fall back to the primitive.
_EXPECTED_KEYS = frozenset(
    {
        "path",
        "size",
        "content_id",
        "mime_type",
        "created_at",
        "modified_at",
        "version",
        "zone_id",
        "owner_id",
        "entry_type",
        "target_zone_id",
        "ttl_seconds",
        "last_writer_address",
        "link_target",
        "gen",
    }
)


def test_readdir_details_returns_full_filemetadata_projection(tmp_path):
    nx = make_test_nexus(tmp_path, is_admin=True)
    try:
        nx.sys_write("/alpha.txt", b"alpha")
        nx.sys_write("/beta.txt", b"beta-content")

        rows = nx.sys_readdir("/", recursive=True, details=True)
        by_path = {r["path"]: r for r in rows}

        assert "/alpha.txt" in by_path, f"alpha.txt missing from {list(by_path)}"
        entry = by_path["/alpha.txt"]

        # Full projection — no FileMetadata field omitted.
        assert set(entry) == _EXPECTED_KEYS, (
            f"detail dict keys diverge from FileMetadata: "
            f"missing={_EXPECTED_KEYS - set(entry)}, extra={set(entry) - _EXPECTED_KEYS}"
        )

        # Spot-check the fields the metastore_list_paginated callers rely on.
        assert entry["size"] == len(b"alpha")
        assert entry["content_id"]
        assert entry["entry_type"] == 0  # DT_REG
        assert entry["ttl_seconds"] == 0.0
        # created_at / modified_at are ISO-8601 strings (JSON-safe over RPC).
        assert isinstance(entry["modified_at"], str)
    finally:
        nx.close()
