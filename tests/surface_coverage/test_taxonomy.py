"""Taxonomy: layered architecture + classification rules."""

import pytest

from scripts.surface_coverage.taxonomy import (
    BRICK_CATEGORIES,
    LAYERS,
    MODULES,
    all_module_ids,
    bricks_by_category,
    classify_op_id,
    modules_by_layer,
)


def test_layers_in_canonical_order():
    assert LAYERS == ("transport", "cross", "brick", "deployment", "nexus_fs", "rust_kernel")


def test_every_module_has_known_layer():
    for m in MODULES:
        assert m.layer in LAYERS, f"{m.id} has unknown layer {m.layer!r}"


def test_no_duplicate_module_ids():
    ids = [m.id for m in MODULES]
    assert len(ids) == len(set(ids))


def test_depends_on_targets_exist():
    ids = all_module_ids()
    for m in MODULES:
        for dep in m.depends_on:
            assert dep in ids, f"{m.id} depends on unknown module {dep}"


def test_every_brick_in_a_category():
    brick_ids = {m.id for m in MODULES if m.layer == "brick"}
    categorized = {bid for ids in BRICK_CATEGORIES.values() for bid in ids}
    assert brick_ids == categorized, (
        f"bricks not in any category: {brick_ids - categorized}; "
        f"category entries that aren't bricks: {categorized - brick_ids}"
    )


def test_28_bricks_present():
    brick_ids = {m.id for m in MODULES if m.layer == "brick"}
    assert len(brick_ids) == 28, f"expected 28 bricks, got {len(brick_ids)}: {sorted(brick_ids)}"


def test_modules_by_layer_returns_all_layers():
    by_layer = modules_by_layer()
    assert set(by_layer.keys()) == set(LAYERS)
    assert len(by_layer["brick"]) == 28


def test_bricks_by_category_omits_empty():
    cats = bricks_by_category()
    for cat, mods in cats.items():
        assert mods, f"category {cat} has no bricks"


@pytest.mark.parametrize(
    "op_id,expected_module",
    [
        # explicit module prefixes win
        ("rebac.grant", "rebac"),
        ("workspace.snapshot_create", "workspace"),
        ("snapshot.list", "snapshot"),
        ("filesystem.read", "filesystem"),
        # @rpc_expose style
        ("oauth_list_providers", "auth"),
        ("rebac_write", "rebac"),
        ("workspace_create", "workspace"),
        ("snapshot_create", "snapshot"),
        ("audit_log_dump", "agent_log"),
        # bare verbs go to filesystem
        ("read", "filesystem"),
        ("write", "filesystem"),
        ("delete", "filesystem"),
        # sys_* syscalls -> nexus_fs
        ("sys_read", "nexus_fs"),
        ("sys_write", "nexus_fs"),
        # transports
        ("share_link.create", "share_link"),
        ("create.share_link", "share_link"),  # substring
        ("upload_chunk", "upload"),
        ("workflow_trigger", "workflows"),
        ("task_run", "task_manager"),
        ("agent_register", "agent_log"),
        # uncategorized fallback
        ("nonsense_xyz_no_match", "uncategorized"),
    ],
)
def test_classify_op_id(op_id, expected_module):
    assert classify_op_id(op_id) == expected_module


def test_deployment_layer_has_5_modules():
    deployment = [m for m in MODULES if m.layer == "deployment"]
    assert len(deployment) == 5
    assert {m.id for m in deployment} == {"hub", "federation", "zone", "daemon", "raft"}


def test_layers_include_deployment():
    assert "deployment" in LAYERS


@pytest.mark.parametrize(
    "op_id,expected_module",
    [
        ("federation.client_whoami", "federation"),
        ("federation.create_zone", "federation"),  # "federation" wins over "zone"
        ("zone.list", "zone"),
        ("create_zone", "zone"),
        ("hub.admin", "hub"),
        ("hub_admin_tool", "hub"),
        ("daemon.enroll", "daemon"),
        ("raft_node_join", "raft"),
    ],
)
def test_classify_deployment(op_id, expected_module):
    assert classify_op_id(op_id) == expected_module
