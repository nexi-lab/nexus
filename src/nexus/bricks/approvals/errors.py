"""Approval brick error hierarchy."""


class ApprovalError(Exception):
    """Base class for all approval-flow errors."""


class ApprovalDenied(ApprovalError):
    """Raised when a request was denied (operator reject or auto-deny)."""

    def __init__(self, request_id: str, reason: str) -> None:
        self.request_id = request_id
        self.reason = reason
        super().__init__(f"approval {request_id} denied: {reason}")


class ApprovalTimeout(ApprovalError):
    """Raised when a request hit auto-deny TTL before any decision."""

    def __init__(self, request_id: str, timeout_seconds: float) -> None:
        self.request_id = request_id
        self.timeout_seconds = timeout_seconds
        super().__init__(f"approval {request_id} timed out after {timeout_seconds}s")


class GatewayClosed(ApprovalError):
    """Raised when the approval pipeline cannot reach Postgres."""
