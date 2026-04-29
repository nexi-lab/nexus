import pytest

from nexus.bricks.approvals.errors import (
    ApprovalDenied,
    ApprovalError,
    ApprovalTimeout,
    GatewayClosed,
)


def test_subclass_hierarchy():
    assert issubclass(ApprovalDenied, ApprovalError)
    assert issubclass(ApprovalTimeout, ApprovalError)
    assert issubclass(GatewayClosed, ApprovalError)


def test_approval_denied_carries_request_id_and_reason():
    err = ApprovalDenied(request_id="req_x", reason="rejected by operator")
    assert err.request_id == "req_x"
    assert err.reason == "rejected by operator"
    assert "req_x" in str(err)


def test_gateway_closed_chains_cause():
    inner = RuntimeError("db down")
    err = GatewayClosed("could not insert pending row")
    err.__cause__ = inner
    with pytest.raises(GatewayClosed) as excinfo:
        raise err
    assert excinfo.value.__cause__ is inner
