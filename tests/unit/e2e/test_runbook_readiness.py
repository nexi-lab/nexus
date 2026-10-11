"""Readiness polling must use its budget across transient RPC failures."""

from types import SimpleNamespace

import grpc
import pytest

from tests.e2e.docker import runbook_helpers as helpers


class RpcFailure(grpc.RpcError):
    def __init__(self, status: grpc.StatusCode):
        self.status = status

    def code(self):
        return self.status

    def details(self):
        return "probe failed"


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(helpers, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    return now


@pytest.mark.parametrize("status", [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED])
def test_transient_rpc_resets_consecutive_successes(monkeypatch, clock, status):
    replies = iter([{}, RpcFailure(status), {}, {}])
    calls = []

    def stat(*args, **kwargs):
        calls.append(kwargs)
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(helpers, "vfs_stat", stat)
    helpers.wait_zone_ready("joiner", "sharedzone", timeout=10)
    assert len(calls) == 4
    assert clock[0] == 1.5


def test_repeated_rpc_timeouts_stop_at_the_total_budget(monkeypatch, clock):
    calls = []

    def stat(*args, **kwargs):
        calls.append(kwargs["timeout"])
        clock[0] += kwargs["timeout"]
        raise RpcFailure(grpc.StatusCode.DEADLINE_EXCEEDED)

    monkeypatch.setattr(helpers, "vfs_stat", stat)
    with pytest.raises(pytest.fail.Exception, match="DEADLINE_EXCEEDED"):
        helpers.wait_zone_ready("joiner", "sharedzone", timeout=6)
    assert calls == [5, 0.5]
    assert clock[0] == 6


@pytest.mark.parametrize(
    "status",
    [grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.INTERNAL],
)
def test_permanent_rpc_refusal_is_not_retried(monkeypatch, clock, status):
    failure = RpcFailure(status)

    def stat(*args, **kwargs):
        raise failure

    monkeypatch.setattr(helpers, "vfs_stat", stat)
    with pytest.raises(RpcFailure) as error:
        helpers.wait_zone_ready("joiner", "sharedzone", timeout=6)
    assert error.value is failure
    assert clock[0] == 0


@pytest.mark.parametrize("status", [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED])
def test_catch_up_waits_through_transport_recovery_and_missing_state(monkeypatch, clock, status):
    replies = iter([RpcFailure(status), {"result": {"found": False}}, {"result": {"found": True}}])
    calls = []

    def stat(*args, **kwargs):
        calls.append(kwargs)
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(helpers, "vfs_stat", stat)
    helpers.wait_nodes_caught_up(["joiner"], "sharedzone", probe_path="/shared/write", timeout=10)
    assert len(calls) == 3
    assert clock[0] == 1


def test_catch_up_timeouts_share_the_remaining_budget_across_nodes(monkeypatch, clock):
    calls = []

    def stat(*args, **kwargs):
        calls.append((args[0], kwargs["timeout"]))
        clock[0] += kwargs["timeout"]
        raise RpcFailure(grpc.StatusCode.DEADLINE_EXCEEDED)

    monkeypatch.setattr(helpers, "vfs_stat", stat)
    with pytest.raises(pytest.fail.Exception, match="DEADLINE_EXCEEDED"):
        helpers.wait_nodes_caught_up(
            ["founder", "joiner", "witness"], "sharedzone", probe_path="/shared/write", timeout=6
        )
    assert calls == [("founder", 5), ("joiner", 1)]
    assert clock[0] == 6


@pytest.mark.parametrize(
    "status",
    [grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.INTERNAL],
)
def test_catch_up_does_not_retry_permanent_rpc_failures(monkeypatch, clock, status):
    failure = RpcFailure(status)

    def stat(*args, **kwargs):
        raise failure

    monkeypatch.setattr(helpers, "vfs_stat", stat)
    with pytest.raises(RpcFailure) as error:
        helpers.wait_nodes_caught_up(
            ["joiner"], "sharedzone", probe_path="/shared/write", timeout=6
        )
    assert error.value is failure
    assert clock[0] == 0


def test_catch_up_requires_the_path_in_every_node_and_zone(monkeypatch, clock):
    calls = []

    def stat(target, path, *, zone_id, **kwargs):
        calls.append((target, zone_id))
        found = len(calls) != 4
        return {"result": {"found": found}}

    monkeypatch.setattr(helpers, "vfs_stat", stat)
    helpers.wait_nodes_caught_up(
        ["founder", "joiner"], ["alpha", "beta"], probe_path="/shared/write", timeout=6
    )
    assert (
        calls
        == [("founder", "alpha"), ("founder", "beta"), ("joiner", "alpha"), ("joiner", "beta")] * 2
    )
    assert clock[0] == 0.5


@pytest.mark.parametrize(
    ("nodes", "zones", "path"),
    [([], "sharedzone", "/file"), (["node"], [], "/file"), (["node"], "sharedzone", "")],
)
def test_catch_up_cannot_succeed_without_a_probe(monkeypatch, clock, nodes, zones, path):
    def stat(*args, **kwargs):
        pytest.fail("invalid readiness inputs must not send an RPC")

    monkeypatch.setattr(helpers, "vfs_stat", stat)
    with pytest.raises(ValueError, match="written probe path"):
        helpers.wait_nodes_caught_up(nodes, zones, probe_path=path)
    assert clock[0] == 0


@pytest.mark.parametrize("status", [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED])
def test_catch_up_handles_restart_errors_over_a_real_grpc_channel(status):
    from concurrent.futures import ThreadPoolExecutor

    from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc

    calls = []

    class RestartingVFS(vfs_pb2_grpc.NexusVFSServiceServicer):
        def Stat(self, request, context):
            calls.append(request.path)
            if len(calls) == 1:
                context.abort(status, "restart replay in progress")
            return vfs_pb2.StatResponse(found=True)

    with ThreadPoolExecutor(max_workers=1) as executor:
        server = grpc.server(executor)
        vfs_pb2_grpc.add_NexusVFSServiceServicer_to_server(RestartingVFS(), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            helpers.wait_nodes_caught_up(
                [f"127.0.0.1:{port}"], "sharedzone", probe_path="/shared/write", timeout=10
            )
            assert calls == ["/shared/write", "/shared/write"]
        finally:
            server.stop(0).wait(timeout=5)
