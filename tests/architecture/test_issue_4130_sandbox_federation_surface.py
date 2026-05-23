"""Coverage contract tests for the sandbox workspace + hub federation story (#4130)."""

from __future__ import annotations

from pathlib import Path

from scripts.surface_coverage.schema import PerfClass, ProfileStatus, load_yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COVERAGE_YAML = _REPO_ROOT / "docs/surface-coverage/api-rpc-surface-coverage.yaml"
_GAPS_YAML = _REPO_ROOT / "docs/surface-coverage/api-rpc-surface-gaps.yaml"
_USER_GUIDE = _REPO_ROOT / "docs/guides/user-guide.md"
_BENCHMARK = _REPO_ROOT / "tests/benchmarks/bench_sandbox_federation_latency.py"

OWNING_ISSUE = 4130

FEDERATION_CONTROL_ROWS = {
    "federation.client_whoami",
    "federation.cluster_info",
    "federation.list_zones",
    "hub.status",
}


def _operations_by_id():
    return {op.id: op for op in load_yaml(_COVERAGE_YAML).operations}


def test_issue_4130_control_rows_have_docs_tests_and_perf_classification() -> None:
    ops = _operations_by_id()

    missing = sorted(FEDERATION_CONTROL_ROWS - set(ops))
    assert not missing

    for op_id in sorted(FEDERATION_CONTROL_ROWS):
        op = ops[op_id]
        assert op.owning_issue == OWNING_ISSUE, op_id
        assert op.summary, op_id
        assert op.usage_example, op_id
        assert op.correctness_test, op_id
        assert op.perf_class in {PerfClass.CONTROL, PerfClass.SETUP}, op_id
        assert op.perf_link, op_id
        assert op.gap_issue is None, op_id

    assert ops["federation.client_whoami"].profiles["sandbox"] == ProfileStatus.SUPPORTED
    assert ops["federation.cluster_info"].profiles["sandbox"] == ProfileStatus.SUPPORTED
    assert ops["federation.list_zones"].profiles["sandbox"] == ProfileStatus.SUPPORTED

    hub_status = ops["hub.status"]
    assert hub_status.transports, "hub.status must point at the implemented CLI/MCP surface"
    assert hub_status.profiles["full"] == ProfileStatus.SUPPORTED
    assert hub_status.profiles["sandbox"] != ProfileStatus.MISSING_NEEDED


def test_issue_4130_hot_paths_link_remote_read_and_fanout_benchmarks() -> None:
    ops = _operations_by_id()

    read = ops["nexus_fs.sys_read"]
    assert read.profiles["sandbox"] == ProfileStatus.SUPPORTED
    assert "test_sandbox_federation_e2e.py" in read.correctness_test
    assert "bench_sandbox_federation_latency.py::TestSandboxFederationReadLatency" in (
        read.perf_link or ""
    )

    write = ops["filesystem.write"]
    assert write.profiles["sandbox"] == ProfileStatus.SUPPORTED
    assert "test_remote_zone.py" in write.correctness_test
    assert "test_sandbox_federation_e2e.py" in write.correctness_test
    assert "bench_permission_hotpath.py" in (write.perf_link or "")

    search = ops["semantic.search"]
    assert search.profiles["sandbox"] == ProfileStatus.SUPPORTED
    assert "test_federated_search.py::TestRemoteZoneSearch" in search.correctness_test
    assert "bench_sandbox_federation_latency.py::TestFederatedSearchFanoutLatency" in (
        search.perf_link or ""
    )

    assert _BENCHMARK.exists()


def test_issue_4130_user_guide_contains_federation_workflow_and_gap_verdict() -> None:
    text = _USER_GUIDE.read_text(encoding="utf-8")

    for needle in [
        "Sandbox local + company hub federation workflow",
        'nexus up --profile sandbox --workspace ~/app --hub-url grpc://hub.example.com:2028 --hub-token "$NEXUS_HUB_TOKEN"',
        "federation_client_whoami",
        "/zone/local",
        "/zone/company",
        "/zone/shared",
        "nexus hub status --detail --json",
        'nexus hub status --remote https://hub.example.com/mcp --admin-token "$NEXUS_HUB_ADMIN_TOKEN" --json',
        "nexus federation info <zone-id>",
        "semantic_degraded",
        "zone_qualified_path",
        "tests/e2e/self_contained/cli/test_sandbox_federation_e2e.py",
        "tests/benchmarks/bench_sandbox_federation_latency.py",
        "Missing-surface gate verdict",
    ]:
        assert needle in text


def test_issue_4130_hub_status_is_not_listed_as_missing_surface() -> None:
    text = _GAPS_YAML.read_text(encoding="utf-8")

    assert "id: hub.status" not in text
