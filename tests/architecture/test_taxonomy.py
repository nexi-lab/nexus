"""Taxonomy: classification rules + module list invariants."""

import pytest

from scripts.surface_coverage.taxonomy import (
    CATEGORIES,
    MODULES,
    all_module_ids,
    classify_op_id,
    module_categories,
)


def test_every_module_in_a_category():
    seen = {mid for mids in CATEGORIES.values() for mid in mids}
    declared = {m.id for m in MODULES}
    assert seen == declared, f"declared - categorized: {declared - seen}; extra: {seen - declared}"


def test_no_duplicate_module_ids():
    ids = [m.id for m in MODULES]
    assert len(ids) == len(set(ids))


def test_depends_on_targets_exist():
    ids = all_module_ids()
    for m in MODULES:
        for dep in m.depends_on:
            assert dep in ids, f"{m.id} depends on unknown module {dep}"


@pytest.mark.parametrize(
    "op_id,expected_module",
    [
        ("fs.read", "fs"),
        ("rebac.grant", "rebac"),
        ("workspace.snapshot_create", "workspace"),
        ("kernel.read", "kernel"),  # explicit kernel prefix
        ("oauth_list_providers", "oauth"),  # rpc_expose-style
        ("oauth.list_providers", "oauth"),
        ("access.share_link", "share_link"),  # access. -> share_link via "share_link" substring
        ("create.share_link", "share_link"),
        ("snapshot.list", "snapshot"),
        ("workspace_create", "workspace"),
        ("read", "fs"),  # bare verb in fs family
        ("write", "fs"),
        ("ping", "kernel"),  # uncategorized -> kernel
        ("unknown_thing", "kernel"),
        ("audit_log_dump", "audit"),
        ("mounts_list", "mounts"),
        ("mcp_tool_invoke", "mcp"),
        ("semantic_query", "semantic"),
        ("search.grep", "search"),
    ],
)
def test_classify_op_id(op_id, expected_module):
    assert classify_op_id(op_id) == expected_module


def test_module_categories_returns_module_objects():
    cats = module_categories()
    fs_mod = cats["Data plane"][0]
    assert fs_mod.id == "fs"
    assert fs_mod.name == "Filesystem"
