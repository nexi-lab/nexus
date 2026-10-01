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
