"""Coverage contract tests for the full-profile ReBAC story (#4134)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.surface_coverage.schema import load_yaml
from scripts.surface_coverage.taxonomy import classify_op_id

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COVERAGE_YAML = _REPO_ROOT / "docs/surface-coverage/api-rpc-surface-coverage.yaml"
_REBAC_SERVICE = _REPO_ROOT / "src/nexus/bricks/rebac/rebac_service.py"

OWNING_ISSUE = 4134

REBAC_RPC_METHOD_TO_OP = {
    "rebac_create": "rebac.create",
    "rebac_check": "rebac.check",
    "rebac_expand": "rebac.expand",
    "rebac_explain": "rebac.explain",
    "rebac_check_batch": "rebac.check_batch",
    "rebac_delete": "rebac.delete",
    "rebac_list_tuples": "rebac.list_tuples",
    "rebac_list_objects": "rebac.list_objects",
    "set_rebac_option": "set.rebac_option",
    "get_rebac_option": "get.rebac_option",
    "register_namespace": "register.namespace",
    "get_namespace": "get.namespace",
    "namespace_list": "namespace.list",
    "namespace_delete": "namespace.delete",
    "rebac_expand_with_privacy": "rebac.expand_with_privacy",
    "grant_consent": "grant.consent",
    "revoke_consent": "revoke.consent",
    "make_public": "make.public",
    "make_private": "make.private",
    "share_with_user": "share.with_user",
    "share_with_group": "share.with_group",
    "revoke_share": "revoke.share",
    "revoke_share_by_id": "revoke.share_by_id",
    "list_outgoing_shares": "list.outgoing_shares",
    "list_incoming_shares": "list.incoming_shares",
    "get_dynamic_viewer_config": "get.dynamic_viewer_config",
    "read_with_dynamic_viewer": "read.with_dynamic_viewer",
}


def _rpc_exposed_methods() -> set[str]:
    module = ast.parse(_REBAC_SERVICE.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "ReBACService":
            names: set[str] = set()
            for item in node.body:
                if not isinstance(item, ast.AsyncFunctionDef | ast.FunctionDef):
                    continue
                for decorator in item.decorator_list:
                    if (
                        (
                            isinstance(decorator, ast.Call)
                            and getattr(decorator.func, "id", "") == "rpc_expose"
                        )
                        or isinstance(decorator, ast.Name)
                        and decorator.id == "rpc_expose"
                    ):
                        names.add(item.name)
            return names
    raise AssertionError("ReBACService class not found")


@pytest.mark.parametrize("op_id", sorted(set(REBAC_RPC_METHOD_TO_OP.values())))
def test_rebac_story_rpc_rows_classify_to_rebac(op_id: str) -> None:
    assert classify_op_id(op_id) == "rebac"


def test_rebac_story_tracks_every_exposed_rebac_rpc() -> None:
    assert _rpc_exposed_methods() == set(REBAC_RPC_METHOD_TO_OP)


def test_rebac_story_rows_have_owner_tests_perf_and_gaps() -> None:
    coverage = load_yaml(_COVERAGE_YAML)
    by_id = {op.id: op for op in coverage.operations}

    for op_id in sorted(REBAC_RPC_METHOD_TO_OP.values()):
        op = by_id[op_id]
        assert op.module == "rebac", op_id
        assert op.owning_issue == OWNING_ISSUE, op_id
        assert op.summary, op_id
        assert op.usage_example, op_id
        assert op.correctness_test, op_id
        assert op.perf_class is not None, op_id
        assert op.perf_link, op_id
        assert op.gap_issue is None, op_id

    assert "rebac.cli_share_privacy_dynamic" not in by_id
