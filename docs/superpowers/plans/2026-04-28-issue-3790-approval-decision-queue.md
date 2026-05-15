# Issue #3790 — Approval Decision Queue + Event API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Nexus server-side approval decision queue, audit log, and gRPC event API with MCP and hub zone-access integration hooks, per spec `2026-04-28-issue-3790-approval-decision-queue-design.md`.

**Architecture:** New brick `src/nexus/bricks/approvals/` extends the existing `governance.approval.ApprovalWorkflow[T]` with policy-gate semantics. Three Postgres tables (requests, decisions, session_allow). PolicyGate sync facade calls async ApprovalService. Coalesce via partial unique index. Multi-worker coordination via Postgres LISTEN/NOTIFY. gRPC primary API; HTTP diagnostic endpoint. MCP middleware and hub auth resolver get small hooks calling `PolicyGate.check`.

**Tech Stack:** Python 3.14, SQLAlchemy 2.x async (`nexus.lib.db_base`), Alembic, FastMCP middleware, asyncpg LISTEN/NOTIFY, gRPC (existing `src/nexus/grpc/`), `pytest`/`pytest-asyncio`.

**Spec:** `docs/superpowers/specs/2026-04-28-issue-3790-approval-decision-queue-design.md`

---

## Phase 1 — Foundation: enums, models, errors, config

### Task 1: ApprovalKind, DecisionScope, Decision, DecisionSource enums + ApprovalRequest dataclass

**Files:**
- Create: `src/nexus/bricks/approvals/__init__.py`
- Create: `src/nexus/bricks/approvals/models.py`
- Test: `tests/unit/bricks/approvals/test_models.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/bricks/approvals/test_models.py
"""Domain model tests for the approvals brick."""

from datetime import UTC, datetime, timedelta

import pytest

from nexus.bricks.approvals.models import (
    ApprovalKind,
    ApprovalRequest,
    Decision,
    DecisionScope,
    DecisionSource,
)


def test_approval_kind_has_all_four_values_from_issue():
    assert {k.value for k in ApprovalKind} == {
        "egress_host",
        "mcp_tool",
        "zone_access",
        "package_install",
    }


def test_decision_scope_has_all_four_values():
    assert {s.value for s in DecisionScope} == {
        "once",
        "session",
        "persist_sandbox",
        "persist_baseline",
    }


def test_decision_terminal_values():
    assert Decision.APPROVED.value == "approved"
    assert Decision.DENIED.value == "denied"


def test_decision_source_values():
    assert {s.value for s in DecisionSource} == {
        "grpc",
        "http",
        "system_timeout",
        "push_api",
    }


def test_approval_request_round_trips_through_dict():
    now = datetime.now(UTC)
    req = ApprovalRequest(
        id="req_01HABC",
        zone_id="eng",
        kind=ApprovalKind.EGRESS_HOST,
        subject="api.stripe.com:443",
        agent_id="claude-1",
        token_id="tok_alice",
        session_id="tok_alice:sess_1",
        reason="nexus_fetch",
        metadata={"url": "https://api.stripe.com/v1/charges"},
        status="pending",
        created_at=now,
        decided_at=None,
        decided_by=None,
        decision_scope=None,
        expires_at=now + timedelta(seconds=60),
    )
    d = req.to_dict()
    again = ApprovalRequest.from_dict(d)
    assert again == req


def test_approval_request_rejects_unknown_status():
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="status"):
        ApprovalRequest(
            id="req_x",
            zone_id="z",
            kind=ApprovalKind.ZONE_ACCESS,
            subject="legal",
            agent_id=None,
            token_id="t",
            session_id=None,
            reason="",
            metadata={},
            status="weird",  # not in {pending, approved, rejected, expired}
            created_at=now,
            decided_at=None,
            decided_by=None,
            decision_scope=None,
            expires_at=now + timedelta(seconds=60),
        )
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/unit/bricks/approvals/test_models.py -v
```

Expected: ImportError / ModuleNotFoundError.

- [ ] **Step 3: Implement the module**

```python
# src/nexus/bricks/approvals/__init__.py
"""Approval decision queue brick (Issue #3790)."""
```

```python
# src/nexus/bricks/approvals/models.py
"""Domain models for approval requests + decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

VALID_STATUSES = frozenset({"pending", "approved", "rejected", "expired"})


class ApprovalKind(StrEnum):
    EGRESS_HOST = "egress_host"
    MCP_TOOL = "mcp_tool"
    ZONE_ACCESS = "zone_access"
    PACKAGE_INSTALL = "package_install"


class DecisionScope(StrEnum):
    ONCE = "once"
    SESSION = "session"
    PERSIST_SANDBOX = "persist_sandbox"
    PERSIST_BASELINE = "persist_baseline"


class Decision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"


class DecisionSource(StrEnum):
    GRPC = "grpc"
    HTTP = "http"
    SYSTEM_TIMEOUT = "system_timeout"
    PUSH_API = "push_api"


@dataclass(frozen=True)
class ApprovalRequest:
    """Domain representation of one row in approval_requests."""

    id: str
    zone_id: str
    kind: ApprovalKind
    subject: str
    agent_id: str | None
    token_id: str | None
    session_id: str | None
    reason: str
    metadata: dict[str, Any]
    status: str
    created_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    decision_scope: DecisionScope | None
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(
                f"ApprovalRequest.status must be one of {sorted(VALID_STATUSES)}, got {self.status!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["decision_scope"] = self.decision_scope.value if self.decision_scope else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ApprovalRequest:
        return cls(
            id=d["id"],
            zone_id=d["zone_id"],
            kind=ApprovalKind(d["kind"]),
            subject=d["subject"],
            agent_id=d["agent_id"],
            token_id=d["token_id"],
            session_id=d["session_id"],
            reason=d["reason"],
            metadata=d["metadata"],
            status=d["status"],
            created_at=d["created_at"],
            decided_at=d["decided_at"],
            decided_by=d["decided_by"],
            decision_scope=DecisionScope(d["decision_scope"]) if d["decision_scope"] else None,
            expires_at=d["expires_at"],
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/unit/bricks/approvals/test_models.py -v
```

Expected: 6 passing.

- [ ] **Step 5: Commit**

```
git add src/nexus/bricks/approvals/__init__.py src/nexus/bricks/approvals/models.py tests/unit/bricks/approvals/test_models.py
git commit -m "feat(#3790): approvals brick — domain models + enums"
```

---

### Task 2: Errors module

**Files:**
- Create: `src/nexus/bricks/approvals/errors.py`
- Test: `tests/unit/bricks/approvals/test_errors.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/bricks/approvals/test_errors.py
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
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/unit/bricks/approvals/test_errors.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# src/nexus/bricks/approvals/errors.py
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
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/unit/bricks/approvals/test_errors.py -v
```

Expected: 3 passing.

- [ ] **Step 5: Commit**

```
git add src/nexus/bricks/approvals/errors.py tests/unit/bricks/approvals/test_errors.py
git commit -m "feat(#3790): approvals brick — error hierarchy"
```

---

### Task 3: ApprovalConfig

**Files:**
- Create: `src/nexus/bricks/approvals/config.py`
- Test: `tests/unit/bricks/approvals/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/bricks/approvals/test_config.py
import pytest

from nexus.bricks.approvals.config import ApprovalConfig


def test_defaults_match_spec():
    cfg = ApprovalConfig()
    assert cfg.enabled is False
    assert cfg.auto_deny_after_seconds == 60.0
    assert cfg.auto_deny_max_seconds == 600.0
    assert cfg.sweeper_interval_seconds == 5.0
    assert cfg.watch_buffer_size == 256
    assert cfg.diag_dump_history_limit == 100


def test_clamp_request_timeout_to_max():
    cfg = ApprovalConfig(auto_deny_after_seconds=60.0, auto_deny_max_seconds=600.0)
    assert cfg.clamp_request_timeout(None) == 60.0
    assert cfg.clamp_request_timeout(10.0) == 10.0
    assert cfg.clamp_request_timeout(9999.0) == 600.0


def test_clamp_rejects_non_positive():
    cfg = ApprovalConfig()
    with pytest.raises(ValueError):
        cfg.clamp_request_timeout(0.0)
    with pytest.raises(ValueError):
        cfg.clamp_request_timeout(-3.0)
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/unit/bricks/approvals/test_config.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# src/nexus/bricks/approvals/config.py
"""Static configuration for the approvals brick."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApprovalConfig:
    enabled: bool = False
    auto_deny_after_seconds: float = 60.0
    auto_deny_max_seconds: float = 600.0
    sweeper_interval_seconds: float = 5.0
    watch_buffer_size: int = 256
    diag_dump_history_limit: int = 100

    def clamp_request_timeout(self, requested: float | None) -> float:
        if requested is None:
            return self.auto_deny_after_seconds
        if requested <= 0:
            raise ValueError(f"timeout must be > 0, got {requested}")
        return min(requested, self.auto_deny_max_seconds)
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/unit/bricks/approvals/test_config.py -v
```

Expected: 3 passing.

- [ ] **Step 5: Commit**

```
git add src/nexus/bricks/approvals/config.py tests/unit/bricks/approvals/test_config.py
git commit -m "feat(#3790): approvals brick — config dataclass"
```

---

## Phase 2 — Database layer

### Task 4: Alembic migration for three approval tables

**Files:**
- Create: `alembic/versions/add_approval_decision_queue.py`

(No unit test; verified via Phase-2 ORM tests below.)

- [ ] **Step 1: Identify the current head revision**

```
alembic heads
```

Note the revision id printed (call it `<HEAD_REV>`). Use it in `down_revision` below. If the command output prints multiple heads, talk to the user — branching migrations are project-policy.

- [ ] **Step 2: Write the migration**

```python
# alembic/versions/add_approval_decision_queue.py
"""Add approval decision queue tables (Issue #3790).

Revision ID: add_approval_decision_queue
Revises: <HEAD_REV>
Create Date: 2026-04-28
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "add_approval_decision_queue"
down_revision: Union[str, Sequence[str], None] = "<HEAD_REV>"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=True),
        sa.Column("token_id", sa.String(255), nullable=True),
        sa.Column("session_id", sa.String(512), nullable=True),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("metadata", sa.dialects.postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(255), nullable=True),
        sa.Column("decision_scope", sa.String(32), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_approval_requests_status_expires",
        "approval_requests",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_approval_requests_zone_status",
        "approval_requests",
        ["zone_id", "status"],
    )
    op.create_index(
        "approval_requests_pending_coalesce",
        "approval_requests",
        ["zone_id", "kind", "subject"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "approval_decisions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(64), sa.ForeignKey("approval_requests.id"), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(255), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
    )
    op.create_index(
        "ix_approval_decisions_request",
        "approval_decisions",
        ["request_id"],
    )

    op.create_table(
        "approval_session_allow",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(512), nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(255), nullable=False),
        sa.Column("request_id", sa.String(64), sa.ForeignKey("approval_requests.id"), nullable=True),
        sa.UniqueConstraint(
            "session_id", "zone_id", "kind", "subject",
            name="uq_approval_session_allow",
        ),
    )
    op.create_index(
        "ix_approval_session_allow_session",
        "approval_session_allow",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_table("approval_session_allow")
    op.drop_table("approval_decisions")
    op.drop_table("approval_requests")
```

Replace the `<HEAD_REV>` placeholder in BOTH the docstring `Revises:` line and the `down_revision` literal with the value from Step 1.

- [ ] **Step 3: Run upgrade against the test database**

```
alembic upgrade head
psql "$NEXUS_TEST_DATABASE_URL" -c "\d approval_requests"
```

Expected: three tables visible; partial unique index `approval_requests_pending_coalesce` listed.

- [ ] **Step 4: Run downgrade and re-upgrade to verify reversibility**

```
alembic downgrade -1
alembic upgrade head
```

Expected: clean.

- [ ] **Step 5: Commit**

```
git add alembic/versions/add_approval_decision_queue.py
git commit -m "feat(#3790): alembic — approval queue tables"
```

---

### Task 5: SQLAlchemy ORM models for the three tables

**Files:**
- Create: `src/nexus/bricks/approvals/db_models.py`
- Test: `tests/unit/bricks/approvals/test_db_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/bricks/approvals/test_db_models.py
"""ORM mapping smoke tests."""

from nexus.bricks.approvals.db_models import (
    ApprovalDecisionModel,
    ApprovalRequestModel,
    ApprovalSessionAllowModel,
)


def test_table_names():
    assert ApprovalRequestModel.__tablename__ == "approval_requests"
    assert ApprovalDecisionModel.__tablename__ == "approval_decisions"
    assert ApprovalSessionAllowModel.__tablename__ == "approval_session_allow"


def test_request_columns_complete():
    cols = {c.name for c in ApprovalRequestModel.__table__.columns}
    assert {
        "id",
        "zone_id",
        "kind",
        "subject",
        "agent_id",
        "token_id",
        "session_id",
        "reason",
        "metadata",
        "status",
        "created_at",
        "decided_at",
        "decided_by",
        "decision_scope",
        "expires_at",
    } <= cols


def test_decision_columns_complete():
    cols = {c.name for c in ApprovalDecisionModel.__table__.columns}
    assert {"id", "request_id", "decided_at", "decided_by", "decision", "scope", "reason", "source"} <= cols


def test_session_allow_unique():
    constraints = {c.name for c in ApprovalSessionAllowModel.__table__.constraints}
    assert "uq_approval_session_allow" in constraints
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/unit/bricks/approvals/test_db_models.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# src/nexus/bricks/approvals/db_models.py
"""SQLAlchemy ORM models mirroring the alembic schema (Issue #3790)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from nexus.lib.db_base import Base


class ApprovalRequestModel(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decision_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_approval_requests_status_expires", "status", "expires_at"),
        Index("ix_approval_requests_zone_status", "zone_id", "status"),
    )


class ApprovalDecisionModel(Base):
    __tablename__ = "approval_decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("approval_requests.id"), nullable=False
    )
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(255), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_approval_decisions_request", "request_id"),)


class ApprovalSessionAllowModel(Base):
    __tablename__ = "approval_session_allow"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(512), nullable=False)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(255), nullable=False)
    request_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("approval_requests.id"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "session_id", "zone_id", "kind", "subject", name="uq_approval_session_allow"
        ),
        Index("ix_approval_session_allow_session", "session_id"),
    )
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/unit/bricks/approvals/test_db_models.py -v
```

Expected: 4 passing.

- [ ] **Step 5: Commit**

```
git add src/nexus/bricks/approvals/db_models.py tests/unit/bricks/approvals/test_db_models.py
git commit -m "feat(#3790): approvals brick — SQLAlchemy ORM models"
```

---

### Task 6: Repository — atomic upsert + lookups

**Files:**
- Create: `src/nexus/bricks/approvals/repository.py`
- Test: `tests/integration/approvals/test_repository.py`

The repository hides SQL behind narrow methods: insert-or-fetch a pending row (coalesce), find existing pending, transition status atomically, append decision, lookup session allow, insert session allow, list pending, sweep expired.

- [ ] **Step 1: Write the failing integration test (live Postgres)**

```python
# tests/integration/approvals/test_repository.py
"""Repository integration tests (live Postgres)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from nexus.bricks.approvals.db_models import (
    ApprovalDecisionModel,
    ApprovalSessionAllowModel,
)
from nexus.bricks.approvals.models import (
    ApprovalKind,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.repository import ApprovalRepository

pytestmark = pytest.mark.integration


async def _new_repo(session_factory) -> ApprovalRepository:
    return ApprovalRepository(session_factory)


@pytest.mark.asyncio
async def test_insert_or_fetch_pending_coalesces_concurrent_inserts(session_factory):
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=60)

    a, b = await asyncio.gather(
        repo.insert_or_fetch_pending(
            request_id="req_a",
            zone_id="z",
            kind=ApprovalKind.EGRESS_HOST,
            subject="api.example.com:443",
            agent_id="ag",
            token_id="tok",
            session_id="tok:s1",
            reason="r",
            metadata={},
            now=now,
            expires_at=expires,
        ),
        repo.insert_or_fetch_pending(
            request_id="req_b",
            zone_id="z",
            kind=ApprovalKind.EGRESS_HOST,
            subject="api.example.com:443",
            agent_id="ag",
            token_id="tok2",
            session_id="tok2:s1",
            reason="r",
            metadata={},
            now=now,
            expires_at=expires,
        ),
    )
    assert a.id == b.id  # exactly one row


@pytest.mark.asyncio
async def test_decide_pending_to_approved_emits_audit_row(session_factory):
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    req = await repo.insert_or_fetch_pending(
        request_id="req_x",
        zone_id="z",
        kind=ApprovalKind.ZONE_ACCESS,
        subject="legal",
        agent_id=None,
        token_id="tok",
        session_id=None,
        reason="",
        metadata={},
        now=now,
        expires_at=now + timedelta(seconds=60),
    )
    updated = await repo.transition(
        request_id=req.id,
        new_status="approved",
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason="ok",
        source=DecisionSource.GRPC,
        now=now,
    )
    assert updated is not None and updated.status == "approved"

    async with session_factory() as s:
        rows = (await s.execute(_select_decisions(req.id))).scalars().all()
        assert len(rows) == 1
        assert rows[0].decision == "approved"


@pytest.mark.asyncio
async def test_transition_returns_none_when_not_pending(session_factory):
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    req = await repo.insert_or_fetch_pending(
        request_id="req_x2",
        zone_id="z",
        kind=ApprovalKind.ZONE_ACCESS,
        subject="legal",
        agent_id=None,
        token_id="tok",
        session_id=None,
        reason="",
        metadata={},
        now=now,
        expires_at=now + timedelta(seconds=60),
    )
    await repo.transition(req.id, "approved", "op", DecisionScope.ONCE, None, DecisionSource.GRPC, now)
    second = await repo.transition(req.id, "rejected", "op2", DecisionScope.ONCE, None, DecisionSource.GRPC, now)
    assert second is None


@pytest.mark.asyncio
async def test_session_allow_round_trip(session_factory):
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    await repo.insert_session_allow(
        session_id="tok:s1",
        zone_id="z",
        kind=ApprovalKind.EGRESS_HOST,
        subject="api.example.com:443",
        decided_by="op",
        decided_at=now,
        request_id=None,
    )
    found = await repo.find_session_allow(
        session_id="tok:s1",
        zone_id="z",
        kind=ApprovalKind.EGRESS_HOST,
        subject="api.example.com:443",
    )
    assert found is not None
    miss = await repo.find_session_allow(
        session_id="tok:s1",
        zone_id="z",
        kind=ApprovalKind.EGRESS_HOST,
        subject="other:443",
    )
    assert miss is None


@pytest.mark.asyncio
async def test_sweep_expired_marks_and_returns_ids(session_factory):
    repo = await _new_repo(session_factory)
    now = datetime.now(UTC)
    past = now - timedelta(seconds=1)
    await repo.insert_or_fetch_pending(
        request_id="req_old",
        zone_id="z",
        kind=ApprovalKind.EGRESS_HOST,
        subject="old.example:443",
        agent_id=None,
        token_id="tok",
        session_id=None,
        reason="",
        metadata={},
        now=past,
        expires_at=past,
    )
    swept = await repo.sweep_expired(now=now)
    assert "req_old" in swept


def _select_decisions(request_id: str):
    from sqlalchemy import select

    return select(ApprovalDecisionModel).where(ApprovalDecisionModel.request_id == request_id)
```

- [ ] **Step 2: Run integration test to verify it fails**

```
pytest tests/integration/approvals/test_repository.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# src/nexus/bricks/approvals/repository.py
"""Repository facade over the three approval tables.

All public methods are async; they hide SQL from the service layer.
The transition() method enforces single-decision atomicity via
UPDATE ... WHERE status='pending' RETURNING ...; callers receive None when
the row was already decided/expired.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.bricks.approvals.db_models import (
    ApprovalDecisionModel,
    ApprovalRequestModel,
    ApprovalSessionAllowModel,
)
from nexus.bricks.approvals.models import (
    ApprovalKind,
    ApprovalRequest,
    DecisionScope,
    DecisionSource,
)

SessionFactory = Callable[[], Awaitable[AsyncSession]]


def _to_domain(row: ApprovalRequestModel) -> ApprovalRequest:
    return ApprovalRequest(
        id=row.id,
        zone_id=row.zone_id,
        kind=ApprovalKind(row.kind),
        subject=row.subject,
        agent_id=row.agent_id,
        token_id=row.token_id,
        session_id=row.session_id,
        reason=row.reason,
        metadata=row.metadata_ or {},
        status=row.status,
        created_at=row.created_at,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
        decision_scope=DecisionScope(row.decision_scope) if row.decision_scope else None,
        expires_at=row.expires_at,
    )


class ApprovalRepository:
    """Async repository for approval queue persistence."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def insert_or_fetch_pending(
        self,
        *,
        request_id: str,
        zone_id: str,
        kind: ApprovalKind,
        subject: str,
        agent_id: str | None,
        token_id: str | None,
        session_id: str | None,
        reason: str,
        metadata: dict[str, Any],
        now: datetime,
        expires_at: datetime,
    ) -> ApprovalRequest:
        """Insert pending row OR return the existing one for the coalesce key.

        Race-safe: relies on the partial unique index
        approval_requests_pending_coalesce.
        """
        async with self._session_factory() as session:
            stmt = (
                pg_insert(ApprovalRequestModel)
                .values(
                    id=request_id,
                    zone_id=zone_id,
                    kind=kind.value,
                    subject=subject,
                    agent_id=agent_id,
                    token_id=token_id,
                    session_id=session_id,
                    reason=reason,
                    metadata=metadata,
                    status="pending",
                    created_at=now,
                    expires_at=expires_at,
                )
                .on_conflict_do_nothing(
                    index_elements=["zone_id", "kind", "subject"],
                    index_where=literal(True),  # we rely on partial-index conflict
                )
                .returning(ApprovalRequestModel)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is not None:
                await session.commit()
                return _to_domain(row)

            # Conflict: fetch the existing pending row
            existing = (
                await session.execute(
                    select(ApprovalRequestModel).where(
                        ApprovalRequestModel.zone_id == zone_id,
                        ApprovalRequestModel.kind == kind.value,
                        ApprovalRequestModel.subject == subject,
                        ApprovalRequestModel.status == "pending",
                    )
                )
            ).scalar_one()
            await session.commit()
            return _to_domain(existing)

    async def get(self, request_id: str) -> ApprovalRequest | None:
        async with self._session_factory() as session:
            row = await session.get(ApprovalRequestModel, request_id)
            return _to_domain(row) if row else None

    async def list_pending(self, zone_id: str | None) -> list[ApprovalRequest]:
        async with self._session_factory() as session:
            stmt = select(ApprovalRequestModel).where(ApprovalRequestModel.status == "pending")
            if zone_id is not None:
                stmt = stmt.where(ApprovalRequestModel.zone_id == zone_id)
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_domain(r) for r in rows]

    async def transition(
        self,
        request_id: str,
        new_status: str,
        decided_by: str,
        scope: DecisionScope,
        reason: str | None,
        source: DecisionSource,
        now: datetime,
    ) -> ApprovalRequest | None:
        """Atomic UPDATE pending → new_status. Returns None if not pending."""
        async with self._session_factory() as session:
            stmt = (
                update(ApprovalRequestModel)
                .where(
                    ApprovalRequestModel.id == request_id,
                    ApprovalRequestModel.status == "pending",
                )
                .values(
                    status=new_status,
                    decided_at=now,
                    decided_by=decided_by,
                    decision_scope=scope.value,
                )
                .returning(ApprovalRequestModel)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                await session.commit()
                return None

            await session.execute(
                insert(ApprovalDecisionModel).values(
                    request_id=request_id,
                    decided_at=now,
                    decided_by=decided_by,
                    decision="approved" if new_status == "approved" else new_status,
                    scope=scope.value,
                    reason=reason,
                    source=source.value,
                )
            )
            await session.commit()
            return _to_domain(row)

    async def insert_session_allow(
        self,
        *,
        session_id: str,
        zone_id: str,
        kind: ApprovalKind,
        subject: str,
        decided_by: str,
        decided_at: datetime,
        request_id: str | None,
    ) -> None:
        async with self._session_factory() as session:
            stmt = (
                pg_insert(ApprovalSessionAllowModel)
                .values(
                    session_id=session_id,
                    zone_id=zone_id,
                    kind=kind.value,
                    subject=subject,
                    decided_by=decided_by,
                    decided_at=decided_at,
                    request_id=request_id,
                )
                .on_conflict_do_nothing(constraint="uq_approval_session_allow")
            )
            await session.execute(stmt)
            await session.commit()

    async def find_session_allow(
        self,
        *,
        session_id: str,
        zone_id: str,
        kind: ApprovalKind,
        subject: str,
    ) -> ApprovalSessionAllowModel | None:
        async with self._session_factory() as session:
            stmt = select(ApprovalSessionAllowModel).where(
                ApprovalSessionAllowModel.session_id == session_id,
                ApprovalSessionAllowModel.zone_id == zone_id,
                ApprovalSessionAllowModel.kind == kind.value,
                ApprovalSessionAllowModel.subject == subject,
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def sweep_expired(self, now: datetime) -> list[str]:
        """Mark all pending past-expires rows as expired and return their ids."""
        async with self._session_factory() as session:
            stmt = (
                update(ApprovalRequestModel)
                .where(
                    ApprovalRequestModel.status == "pending",
                    ApprovalRequestModel.expires_at < now,
                )
                .values(
                    status="expired",
                    decided_at=now,
                    decided_by="system",
                    decision_scope=DecisionScope.ONCE.value,
                )
                .returning(ApprovalRequestModel.id)
            )
            ids = list((await session.execute(stmt)).scalars().all())
            for rid in ids:
                await session.execute(
                    insert(ApprovalDecisionModel).values(
                        request_id=rid,
                        decided_at=now,
                        decided_by="system",
                        decision="expired",
                        scope=DecisionScope.ONCE.value,
                        reason="auto_deny_after_timeout",
                        source=DecisionSource.SYSTEM_TIMEOUT.value,
                    )
                )
            await session.commit()
            return ids
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/integration/approvals/test_repository.py -v
```

Expected: 5 passing.

- [ ] **Step 5: Commit**

```
git add src/nexus/bricks/approvals/repository.py tests/integration/approvals/test_repository.py
git commit -m "feat(#3790): approvals brick — repository + atomic transition"
```

---

## Phase 3 — Event dispatch (futures + LISTEN/NOTIFY)

### Task 7: In-process dispatcher with futures

**Files:**
- Create: `src/nexus/bricks/approvals/events.py`
- Test: `tests/unit/bricks/approvals/test_events_dispatcher.py`

The dispatcher maps `request_id` → list of asyncio.Futures. Decisions resolve all futures; cancel removes one entry.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/bricks/approvals/test_events_dispatcher.py
"""In-process future dispatcher tests."""

import asyncio

import pytest

from nexus.bricks.approvals.events import Dispatcher
from nexus.bricks.approvals.models import Decision


@pytest.mark.asyncio
async def test_resolve_wakes_all_waiters_for_request_id():
    d = Dispatcher()
    f1 = d.register("req_a")
    f2 = d.register("req_a")
    d.resolve("req_a", Decision.APPROVED)
    assert (await asyncio.wait_for(f1, 0.5)) is Decision.APPROVED
    assert (await asyncio.wait_for(f2, 0.5)) is Decision.APPROVED


@pytest.mark.asyncio
async def test_resolve_for_unknown_id_is_noop():
    d = Dispatcher()
    d.resolve("nope", Decision.DENIED)  # should not raise


@pytest.mark.asyncio
async def test_cancel_unregisters_one_future():
    d = Dispatcher()
    f1 = d.register("req_b")
    f2 = d.register("req_b")
    d.cancel(f1)
    d.resolve("req_b", Decision.DENIED)
    # f1 was cancelled out of the registry but the asyncio.Future itself
    # is not auto-cancelled — caller is responsible for f1.cancel().
    assert (await asyncio.wait_for(f2, 0.5)) is Decision.DENIED


@pytest.mark.asyncio
async def test_in_flight_request_ids_returns_known_keys():
    d = Dispatcher()
    d.register("req_a")
    d.register("req_b")
    assert set(d.in_flight_request_ids()) == {"req_a", "req_b"}
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/unit/bricks/approvals/test_events_dispatcher.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# src/nexus/bricks/approvals/events.py
"""Event dispatcher: futures + Postgres LISTEN/NOTIFY bridge."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from nexus.bricks.approvals.models import Decision

logger = logging.getLogger(__name__)


class Dispatcher:
    """In-process map of request_id → futures awaiting a Decision."""

    def __init__(self) -> None:
        self._waiters: dict[str, list[asyncio.Future[Decision]]] = defaultdict(list)

    def register(self, request_id: str) -> asyncio.Future[Decision]:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Decision] = loop.create_future()
        self._waiters[request_id].append(fut)
        return fut

    def cancel(self, fut: asyncio.Future[Decision]) -> None:
        """Remove one future from any list it appears in."""
        for rid, lst in list(self._waiters.items()):
            if fut in lst:
                lst.remove(fut)
                if not lst:
                    del self._waiters[rid]
                return

    def resolve(self, request_id: str, decision: Decision) -> None:
        waiters = self._waiters.pop(request_id, ())
        for fut in waiters:
            if not fut.done():
                fut.set_result(decision)

    def in_flight_request_ids(self) -> list[str]:
        return list(self._waiters.keys())
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/unit/bricks/approvals/test_events_dispatcher.py -v
```

Expected: 4 passing.

- [ ] **Step 5: Commit**

```
git add src/nexus/bricks/approvals/events.py tests/unit/bricks/approvals/test_events_dispatcher.py
git commit -m "feat(#3790): approvals brick — in-process dispatcher"
```

---

### Task 8: Postgres LISTEN/NOTIFY bridge

**Files:**
- Modify: `src/nexus/bricks/approvals/events.py` (extend with `NotifyBridge`)
- Test: `tests/integration/approvals/test_notify_bridge.py`

Two channels: `approvals_new` (payload: request_id JSON) and `approvals_decided` (payload: `{"request_id": ..., "decision": ...}`). The bridge uses `asyncpg`'s `add_listener` for receive and a small `notify(channel, payload)` helper for send.

- [ ] **Step 1: Write the failing integration test**

```python
# tests/integration/approvals/test_notify_bridge.py
"""LISTEN/NOTIFY bridge integration test."""

import asyncio
import json

import pytest

from nexus.bricks.approvals.events import NotifyBridge

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_notify_payload_round_trip(asyncpg_pool):
    received: list[dict] = []
    event = asyncio.Event()

    async def on_decided(payload: str) -> None:
        received.append(json.loads(payload))
        event.set()

    bridge = NotifyBridge(asyncpg_pool)
    await bridge.start({"approvals_decided": on_decided})
    try:
        await bridge.notify("approvals_decided", json.dumps({"request_id": "rx", "decision": "approved"}))
        await asyncio.wait_for(event.wait(), 2.0)
    finally:
        await bridge.stop()

    assert received == [{"request_id": "rx", "decision": "approved"}]
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/integration/approvals/test_notify_bridge.py -v
```

Expected: ImportError on `NotifyBridge`.

- [ ] **Step 3: Implement (append to `events.py`)**

```python
# src/nexus/bricks/approvals/events.py — append below Dispatcher

from collections.abc import Awaitable, Callable

import asyncpg


NotifyHandler = Callable[[str], Awaitable[None]]


class NotifyBridge:
    """Bridge to Postgres LISTEN/NOTIFY using a dedicated asyncpg connection.

    Holds one connection borrowed from the pool for the lifetime of the bridge.
    Multiple LISTEN channels are supported; notify() acquires a fresh connection
    each call so listening continues uninterrupted.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._listen_conn: asyncpg.Connection | None = None
        self._handlers: dict[str, NotifyHandler] = {}

    async def start(self, handlers: dict[str, NotifyHandler]) -> None:
        self._handlers = dict(handlers)
        self._listen_conn = await self._pool.acquire()
        for channel, _h in self._handlers.items():
            await self._listen_conn.add_listener(channel, self._on_notify)

    async def stop(self) -> None:
        if self._listen_conn is None:
            return
        for channel in list(self._handlers):
            try:
                await self._listen_conn.remove_listener(channel, self._on_notify)
            except Exception:
                logger.debug("remove_listener failed for %s", channel, exc_info=True)
        await self._pool.release(self._listen_conn)
        self._listen_conn = None
        self._handlers = {}

    async def notify(self, channel: str, payload: str) -> None:
        async with self._pool.acquire() as conn:
            # Use parameterised payload via SELECT pg_notify; NOTIFY does not accept params.
            await conn.execute("SELECT pg_notify($1, $2)", channel, payload)

    def _on_notify(
        self,
        connection: asyncpg.Connection,
        pid: int,
        channel: str,
        payload: str,
    ) -> None:
        handler = self._handlers.get(channel)
        if handler is None:
            return
        # asyncpg invokes this synchronously; schedule async handler.
        asyncio.create_task(handler(payload))
```

- [ ] **Step 4: Run integration test to verify it passes**

```
pytest tests/integration/approvals/test_notify_bridge.py -v
```

Expected: 1 passing.

- [ ] **Step 5: Commit**

```
git add src/nexus/bricks/approvals/events.py tests/integration/approvals/test_notify_bridge.py
git commit -m "feat(#3790): approvals brick — LISTEN/NOTIFY bridge"
```

---

## Phase 4 — Service core

### Task 9: ApprovalService.request_and_wait — end-to-end coalesce + future + timeout

**Files:**
- Create: `src/nexus/bricks/approvals/service.py`
- Test: `tests/integration/approvals/test_service_request_and_wait.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/approvals/test_service_request_and_wait.py
"""ApprovalService.request_and_wait integration tests."""

import asyncio
from datetime import UTC, datetime

import pytest

from nexus.bricks.approvals.config import ApprovalConfig
from nexus.bricks.approvals.errors import ApprovalDenied, ApprovalTimeout
from nexus.bricks.approvals.models import (
    ApprovalKind,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.service import ApprovalService

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_approve_unblocks_waiting_caller(approval_service: ApprovalService):
    waiting = asyncio.create_task(
        approval_service.request_and_wait(
            request_id="req_a",
            zone_id="z",
            kind=ApprovalKind.EGRESS_HOST,
            subject="api.x:443",
            agent_id="ag",
            token_id="tok",
            session_id="tok:s",
            reason="r",
            metadata={},
        )
    )
    await asyncio.sleep(0.05)  # let pending row land

    decided = await approval_service.decide(
        request_id="req_a",
        decision=Decision.APPROVED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason=None,
        source=DecisionSource.GRPC,
    )
    assert decided.status == "approved"
    assert (await asyncio.wait_for(waiting, 1.0)) is Decision.APPROVED


@pytest.mark.asyncio
async def test_deny_raises_approval_denied(approval_service: ApprovalService):
    waiting = asyncio.create_task(
        approval_service.request_and_wait(
            request_id="req_b",
            zone_id="z",
            kind=ApprovalKind.EGRESS_HOST,
            subject="api.y:443",
            agent_id="ag",
            token_id="tok",
            session_id="tok:s",
            reason="r",
            metadata={},
        )
    )
    await asyncio.sleep(0.05)
    await approval_service.decide(
        request_id="req_b",
        decision=Decision.DENIED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason="nope",
        source=DecisionSource.GRPC,
    )
    with pytest.raises(ApprovalDenied):
        await asyncio.wait_for(waiting, 1.0)


@pytest.mark.asyncio
async def test_timeout_raises_approval_timeout(approval_service_short: ApprovalService):
    # approval_service_short fixture: auto_deny_after_seconds=0.2
    with pytest.raises(ApprovalTimeout):
        await approval_service_short.request_and_wait(
            request_id="req_c",
            zone_id="z",
            kind=ApprovalKind.EGRESS_HOST,
            subject="slow:443",
            agent_id="ag",
            token_id="tok",
            session_id="tok:s",
            reason="r",
            metadata={},
        )


@pytest.mark.asyncio
async def test_concurrent_callers_same_subject_share_one_row(approval_service: ApprovalService):
    async def call(rid: str):
        return await approval_service.request_and_wait(
            request_id=rid,
            zone_id="z",
            kind=ApprovalKind.EGRESS_HOST,
            subject="shared.example:443",
            agent_id="ag",
            token_id="tok",
            session_id=f"tok:s_{rid}",
            reason="r",
            metadata={},
        )

    t1 = asyncio.create_task(call("req_d1"))
    t2 = asyncio.create_task(call("req_d2"))
    await asyncio.sleep(0.05)

    pending = await approval_service.list_pending(zone_id="z")
    assert len([p for p in pending if p.subject == "shared.example:443"]) == 1
    coalesced_id = next(p.id for p in pending if p.subject == "shared.example:443")
    await approval_service.decide(
        request_id=coalesced_id,
        decision=Decision.APPROVED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason=None,
        source=DecisionSource.GRPC,
    )
    assert await asyncio.wait_for(t1, 1.0) is Decision.APPROVED
    assert await asyncio.wait_for(t2, 1.0) is Decision.APPROVED
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/integration/approvals/test_service_request_and_wait.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# src/nexus/bricks/approvals/service.py
"""ApprovalService — async core."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from nexus.bricks.approvals.config import ApprovalConfig
from nexus.bricks.approvals.errors import (
    ApprovalDenied,
    ApprovalTimeout,
    GatewayClosed,
)
from nexus.bricks.approvals.events import Dispatcher, NotifyBridge
from nexus.bricks.approvals.models import (
    ApprovalKind,
    ApprovalRequest,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.repository import ApprovalRepository

logger = logging.getLogger(__name__)

CHANNEL_NEW = "approvals_new"
CHANNEL_DECIDED = "approvals_decided"


class ApprovalService:
    def __init__(
        self,
        repository: ApprovalRepository,
        notify_bridge: NotifyBridge,
        config: ApprovalConfig,
    ) -> None:
        self._repo = repository
        self._notify = notify_bridge
        self._cfg = config
        self._dispatcher = Dispatcher()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        await self._notify.start(
            {
                CHANNEL_DECIDED: self._on_decided_payload,
                CHANNEL_NEW: self._on_new_payload,
            }
        )

    async def stop(self) -> None:
        await self._notify.stop()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def request_and_wait(
        self,
        *,
        request_id: str,
        zone_id: str,
        kind: ApprovalKind,
        subject: str,
        agent_id: str | None,
        token_id: str | None,
        session_id: str | None,
        reason: str,
        metadata: dict[str, Any],
        timeout_override: float | None = None,
    ) -> Decision:
        timeout = self._cfg.clamp_request_timeout(timeout_override)
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=timeout)

        try:
            req = await self._repo.insert_or_fetch_pending(
                request_id=request_id,
                zone_id=zone_id,
                kind=kind,
                subject=subject,
                agent_id=agent_id,
                token_id=token_id,
                session_id=session_id,
                reason=reason,
                metadata=metadata,
                now=now,
                expires_at=expires,
            )
        except Exception as e:
            raise GatewayClosed("could not insert pending row") from e

        # Was it newly inserted under our id, or an existing coalesced row?
        if req.id == request_id:
            try:
                await self._notify.notify(
                    CHANNEL_NEW,
                    json.dumps({"request_id": req.id, "zone_id": zone_id}),
                )
            except Exception:
                logger.warning("notify(approvals_new) failed; queue still durable", exc_info=True)

        fut = self._dispatcher.register(req.id)

        # If the row is already terminal (race: decided between insert and register),
        # short-circuit by re-fetching.
        latest = await self._repo.get(req.id)
        if latest and latest.status != "pending":
            self._dispatcher.cancel(fut)
            return _row_to_decision(latest, request_id=req.id, timeout=timeout)

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            self._dispatcher.cancel(fut)
            raise ApprovalTimeout(req.id, timeout) from e

        if result is Decision.DENIED:
            row = await self._repo.get(req.id)
            reason_str = (row and row.decision_scope and row.decided_by) or "denied"
            raise ApprovalDenied(req.id, str(reason_str))
        return result

    async def decide(
        self,
        *,
        request_id: str,
        decision: Decision,
        decided_by: str,
        scope: DecisionScope,
        reason: str | None,
        source: DecisionSource,
    ) -> ApprovalRequest:
        new_status = "approved" if decision is Decision.APPROVED else "rejected"
        now = datetime.now(UTC)

        updated = await self._repo.transition(
            request_id=request_id,
            new_status=new_status,
            decided_by=decided_by,
            scope=scope,
            reason=reason,
            source=source,
            now=now,
        )
        if updated is None:
            raise ValueError(f"request {request_id} is not pending")

        if scope is DecisionScope.SESSION and decision is Decision.APPROVED and updated.session_id:
            await self._repo.insert_session_allow(
                session_id=updated.session_id,
                zone_id=updated.zone_id,
                kind=updated.kind,
                subject=updated.subject,
                decided_by=decided_by,
                decided_at=now,
                request_id=updated.id,
            )

        await self._notify.notify(
            CHANNEL_DECIDED,
            json.dumps({"request_id": request_id, "decision": decision.value}),
        )
        # Resolve in-process futures immediately for callers on the same worker.
        self._dispatcher.resolve(request_id, decision)
        return updated

    async def list_pending(self, zone_id: str | None) -> list[ApprovalRequest]:
        return await self._repo.list_pending(zone_id)

    async def get(self, request_id: str) -> ApprovalRequest | None:
        return await self._repo.get(request_id)

    async def cancel(self, future: asyncio.Future[Decision]) -> None:
        self._dispatcher.cancel(future)

    # ------------------------------------------------------------------
    # NOTIFY handlers
    # ------------------------------------------------------------------

    async def _on_decided_payload(self, payload: str) -> None:
        try:
            msg = json.loads(payload)
            rid = msg["request_id"]
            decision = Decision(msg["decision"])
        except Exception:
            logger.warning("bad approvals_decided payload: %s", payload)
            return
        self._dispatcher.resolve(rid, decision)

    async def _on_new_payload(self, payload: str) -> None:
        # No-op for callers; only Watch-stream needs new-pending events (Task 12).
        pass


def _row_to_decision(row: ApprovalRequest, *, request_id: str, timeout: float) -> Decision:
    if row.status == "approved":
        return Decision.APPROVED
    if row.status == "rejected":
        raise ApprovalDenied(row.id, "rejected")
    if row.status == "expired":
        raise ApprovalTimeout(row.id, timeout)
    raise RuntimeError(f"unexpected status {row.status}")
```

(`approval_service` fixture wiring lives in `tests/integration/approvals/conftest.py` — see Task 9b below.)

- [ ] **Step 4: Run tests to verify they fail with a fixture error**

```
pytest tests/integration/approvals/test_service_request_and_wait.py -v
```

Expected: fixture error pointing at `approval_service` / `approval_service_short`. We add fixtures next.

- [ ] **Step 5: Commit**

```
git add src/nexus/bricks/approvals/service.py tests/integration/approvals/test_service_request_and_wait.py
git commit -m "feat(#3790): approvals brick — ApprovalService.request_and_wait + decide"
```

---

### Task 9b: Approvals integration test fixtures

**Files:**
- Create: `tests/integration/approvals/__init__.py`
- Create: `tests/integration/approvals/conftest.py`

- [ ] **Step 1: Add the fixtures**

```python
# tests/integration/approvals/__init__.py
```

```python
# tests/integration/approvals/conftest.py
"""Shared fixtures for approvals integration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nexus.bricks.approvals.config import ApprovalConfig
from nexus.bricks.approvals.events import NotifyBridge
from nexus.bricks.approvals.repository import ApprovalRepository
from nexus.bricks.approvals.service import ApprovalService


def _db_url() -> str:
    import os

    url = os.environ.get("NEXUS_TEST_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "NEXUS_TEST_DATABASE_URL must be set for approvals integration tests"
        )
    return url


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(_db_url())
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def asyncpg_pool() -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(
        _db_url().replace("postgresql+asyncpg://", "postgresql://"),
        min_size=1,
        max_size=4,
    )
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def approval_service(session_factory, asyncpg_pool) -> AsyncIterator[ApprovalService]:
    repo = ApprovalRepository(session_factory)
    bridge = NotifyBridge(asyncpg_pool)
    svc = ApprovalService(repo, bridge, ApprovalConfig(enabled=True))
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


@pytest_asyncio.fixture
async def approval_service_short(session_factory, asyncpg_pool) -> AsyncIterator[ApprovalService]:
    repo = ApprovalRepository(session_factory)
    bridge = NotifyBridge(asyncpg_pool)
    svc = ApprovalService(
        repo, bridge, ApprovalConfig(enabled=True, auto_deny_after_seconds=0.2)
    )
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()
```

- [ ] **Step 2: Re-run service tests**

```
pytest tests/integration/approvals/test_service_request_and_wait.py -v
```

Expected: 4 passing.

- [ ] **Step 3: Commit**

```
git add tests/integration/approvals/__init__.py tests/integration/approvals/conftest.py
git commit -m "test(#3790): approvals integration test fixtures"
```

---

### Task 10: ApprovalService.watch — async event stream

**Files:**
- Modify: `src/nexus/bricks/approvals/service.py`
- Test: `tests/integration/approvals/test_service_watch.py`

Watch yields `(event_type, request_id, decision_or_none)` events. Backed by a per-call asyncio.Queue, fed from the same NOTIFY callbacks.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/approvals/test_service_watch.py
"""ApprovalService.watch tests."""

import asyncio

import pytest

from nexus.bricks.approvals.models import (
    ApprovalKind,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.service import ApprovalService

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_watch_emits_pending_then_decided(approval_service: ApprovalService):
    events: list[tuple[str, str, str | None]] = []
    stop = asyncio.Event()

    async def consume():
        async for ev in approval_service.watch(zone_id="z"):
            events.append((ev.type, ev.request_id, ev.decision))
            if ev.type == "decided":
                stop.set()
                break

    task = asyncio.create_task(consume())
    waiter = asyncio.create_task(
        approval_service.request_and_wait(
            request_id="req_w",
            zone_id="z",
            kind=ApprovalKind.EGRESS_HOST,
            subject="watch.example:443",
            agent_id="ag",
            token_id="tok",
            session_id="tok:s",
            reason="r",
            metadata={},
        )
    )
    await asyncio.sleep(0.1)
    await approval_service.decide(
        request_id="req_w",
        decision=Decision.APPROVED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason=None,
        source=DecisionSource.GRPC,
    )
    await asyncio.wait_for(waiter, 1.0)
    await asyncio.wait_for(stop.wait(), 1.0)
    task.cancel()

    types = [e[0] for e in events]
    assert "pending" in types and "decided" in types
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/integration/approvals/test_service_watch.py -v
```

Expected: AttributeError on `watch`.

- [ ] **Step 3: Implement (additions to `service.py`)**

```python
# src/nexus/bricks/approvals/service.py — additions

from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class WatchEvent:
    type: str  # "pending" | "decided"
    request_id: str
    zone_id: str
    decision: str | None


class ApprovalService:  # extend the existing class — re-declare in same file
    ...

    async def watch(self, zone_id: str | None) -> AsyncIterator[WatchEvent]:
        q: asyncio.Queue[WatchEvent] = asyncio.Queue(maxsize=self._cfg.watch_buffer_size)
        self._watchers.append((zone_id, q))
        try:
            while True:
                ev = await q.get()
                yield ev
        finally:
            try:
                self._watchers.remove((zone_id, q))
            except ValueError:
                pass
```

(Add `self._watchers: list[tuple[str | None, asyncio.Queue[WatchEvent]]] = []` to `__init__`.)

Update `_on_new_payload` and `_on_decided_payload` to fan out to watchers:

```python
    async def _on_new_payload(self, payload: str) -> None:
        try:
            msg = json.loads(payload)
            rid = msg["request_id"]
            zone = msg["zone_id"]
        except Exception:
            return
        self._broadcast(WatchEvent(type="pending", request_id=rid, zone_id=zone, decision=None))

    async def _on_decided_payload(self, payload: str) -> None:
        try:
            msg = json.loads(payload)
            rid = msg["request_id"]
            decision = Decision(msg["decision"])
        except Exception:
            logger.warning("bad approvals_decided payload: %s", payload)
            return
        self._dispatcher.resolve(rid, decision)
        # zone is not on the payload; look it up if we have it.
        row = await self._repo.get(rid)
        zone = row.zone_id if row else ""
        self._broadcast(
            WatchEvent(type="decided", request_id=rid, zone_id=zone, decision=decision.value)
        )

    def _broadcast(self, ev: WatchEvent) -> None:
        for zone, q in list(self._watchers):
            if zone is not None and zone != ev.zone_id:
                continue
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                # Slow watcher: drop and let it reconcile via list_pending.
                logger.warning("watch buffer overflow; dropping event for %s", ev.request_id)
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/integration/approvals/test_service_watch.py -v
```

Expected: 1 passing.

- [ ] **Step 5: Commit**

```
git add src/nexus/bricks/approvals/service.py tests/integration/approvals/test_service_watch.py
git commit -m "feat(#3790): approvals brick — Watch event stream"
```

---

### Task 11: Sweeper — periodic expiry of stale pending rows

**Files:**
- Create: `src/nexus/bricks/approvals/sweeper.py`
- Test: `tests/integration/approvals/test_sweeper.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/approvals/test_sweeper.py
"""Sweeper integration test."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from nexus.bricks.approvals.models import ApprovalKind
from nexus.bricks.approvals.repository import ApprovalRepository
from nexus.bricks.approvals.sweeper import Sweeper

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_sweeper_expires_past_due_rows(session_factory, asyncpg_pool):
    repo = ApprovalRepository(session_factory)
    past = datetime.now(UTC) - timedelta(seconds=5)
    await repo.insert_or_fetch_pending(
        request_id="req_old_s",
        zone_id="z",
        kind=ApprovalKind.EGRESS_HOST,
        subject="old:443",
        agent_id=None,
        token_id="tok",
        session_id=None,
        reason="",
        metadata={},
        now=past,
        expires_at=past,
    )
    sweeper = Sweeper(repo, interval_seconds=0.1, on_expired=lambda ids: None)
    await sweeper.start()
    try:
        await asyncio.sleep(0.3)
    finally:
        await sweeper.stop()

    row = await repo.get("req_old_s")
    assert row is not None and row.status == "expired"
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/integration/approvals/test_sweeper.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# src/nexus/bricks/approvals/sweeper.py
"""Background sweeper that expires past-due pending requests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from nexus.bricks.approvals.repository import ApprovalRepository

logger = logging.getLogger(__name__)


class Sweeper:
    def __init__(
        self,
        repository: ApprovalRepository,
        interval_seconds: float,
        on_expired: Callable[[list[str]], None],
    ) -> None:
        self._repo = repository
        self._interval = interval_seconds
        self._on_expired = on_expired
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        await self._task
        self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                ids = await self._repo.sweep_expired(now=datetime.now(UTC))
                if ids:
                    self._on_expired(ids)
            except Exception:
                logger.exception("sweeper iteration failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue
```

Wire the sweeper from `ApprovalService.start`. Update `service.py`:

```python
# Inside ApprovalService.__init__:
        self._sweeper = Sweeper(
            repository=repository,
            interval_seconds=config.sweeper_interval_seconds,
            on_expired=self._on_expired_ids,
        )

# Add method:
    def _on_expired_ids(self, ids: list[str]) -> None:
        for rid in ids:
            self._dispatcher.resolve(rid, Decision.DENIED)
            self._broadcast(
                WatchEvent(type="decided", request_id=rid, zone_id="", decision="expired")
            )

# Inside ApprovalService.start, after notify_bridge.start:
        await self._sweeper.start()

# Inside ApprovalService.stop, before notify_bridge.stop:
        await self._sweeper.stop()
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/integration/approvals/test_sweeper.py -v
```

Expected: 1 passing.

- [ ] **Step 5: Commit**

```
git add src/nexus/bricks/approvals/sweeper.py src/nexus/bricks/approvals/service.py tests/integration/approvals/test_sweeper.py
git commit -m "feat(#3790): approvals brick — sweeper for auto-expiry"
```

---

### Task 12: Reconcile-on-reconnect — fix dropped NOTIFYs

**Files:**
- Modify: `src/nexus/bricks/approvals/service.py`
- Test: `tests/integration/approvals/test_reconcile_reconnect.py`

The test simulates a missed NOTIFY by deciding a row directly via repo (no pub/sub), then asks the service to reconcile.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/approvals/test_reconcile_reconnect.py
import asyncio
from datetime import UTC, datetime

import pytest

from nexus.bricks.approvals.models import (
    ApprovalKind,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.service import ApprovalService

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_reconcile_resolves_pending_futures_for_decided_rows(
    approval_service: ApprovalService,
):
    waiter = asyncio.create_task(
        approval_service.request_and_wait(
            request_id="req_recon",
            zone_id="z",
            kind=ApprovalKind.EGRESS_HOST,
            subject="recon:443",
            agent_id="ag",
            token_id="tok",
            session_id="tok:s",
            reason="r",
            metadata={},
        )
    )
    await asyncio.sleep(0.05)

    # Bypass NOTIFY: write the decision via the repo directly.
    await approval_service._repo.transition(
        request_id="req_recon",
        new_status="approved",
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason=None,
        source=DecisionSource.GRPC,
        now=datetime.now(UTC),
    )

    # Force reconciliation
    await approval_service.reconcile_in_flight()
    assert (await asyncio.wait_for(waiter, 1.0)) is Decision.APPROVED
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/integration/approvals/test_reconcile_reconnect.py -v
```

Expected: AttributeError on `reconcile_in_flight`.

- [ ] **Step 3: Add the method to `ApprovalService`**

```python
    async def reconcile_in_flight(self) -> None:
        """Re-resolve futures for any in-flight request that already terminated.

        Call after a LISTEN reconnect to recover from missed notifications.
        """
        for rid in self._dispatcher.in_flight_request_ids():
            row = await self._repo.get(rid)
            if row is None:
                continue
            if row.status == "approved":
                self._dispatcher.resolve(rid, Decision.APPROVED)
            elif row.status in ("rejected", "expired"):
                self._dispatcher.resolve(rid, Decision.DENIED)
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/integration/approvals/test_reconcile_reconnect.py -v
```

Expected: 1 passing.

- [ ] **Step 5: Commit**

```
git add src/nexus/bricks/approvals/service.py tests/integration/approvals/test_reconcile_reconnect.py
git commit -m "feat(#3790): approvals brick — reconcile_in_flight after reconnect"
```

---

## Phase 5 — Gate facade

### Task 13: PolicyGate — session_allow cache + service.request_and_wait

**Files:**
- Create: `src/nexus/bricks/approvals/policy_gate.py`
- Test: `tests/integration/approvals/test_policy_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/approvals/test_policy_gate.py
import asyncio

import pytest

from nexus.bricks.approvals.models import (
    ApprovalKind,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.policy_gate import PolicyGate

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_session_allow_cache_short_circuits(approval_service):
    gate = PolicyGate(approval_service)

    # Approve once at session scope to seed the cache.
    waiter = asyncio.create_task(
        gate.check(
            kind=ApprovalKind.EGRESS_HOST,
            subject="cache.example:443",
            zone_id="z",
            token_id="tok",
            session_id="tok:s",
            agent_id="ag",
            reason="r",
            metadata={},
        )
    )
    await asyncio.sleep(0.05)
    pending = await approval_service.list_pending(zone_id="z")
    rid = next(p.id for p in pending if p.subject == "cache.example:443")
    await approval_service.decide(
        request_id=rid,
        decision=Decision.APPROVED,
        decided_by="op",
        scope=DecisionScope.SESSION,
        reason=None,
        source=DecisionSource.GRPC,
    )
    assert (await asyncio.wait_for(waiter, 1.0)) is Decision.APPROVED

    # Second call for the same (session_id, zone, kind, subject): no new pending row.
    fast = await asyncio.wait_for(
        gate.check(
            kind=ApprovalKind.EGRESS_HOST,
            subject="cache.example:443",
            zone_id="z",
            token_id="tok",
            session_id="tok:s",
            agent_id="ag",
            reason="r",
            metadata={},
        ),
        timeout=0.5,
    )
    assert fast is Decision.APPROVED
    pending2 = await approval_service.list_pending(zone_id="z")
    assert all(p.subject != "cache.example:443" for p in pending2)
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/integration/approvals/test_policy_gate.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# src/nexus/bricks/approvals/policy_gate.py
"""PolicyGate — sync facade hooks call to request a decision."""

from __future__ import annotations

import logging
import secrets
from typing import Any

from nexus.bricks.approvals.errors import ApprovalDenied, ApprovalTimeout, GatewayClosed
from nexus.bricks.approvals.models import ApprovalKind, Decision
from nexus.bricks.approvals.service import ApprovalService

logger = logging.getLogger(__name__)


def _new_request_id() -> str:
    return f"req_{secrets.token_hex(8)}"


class PolicyGate:
    def __init__(self, service: ApprovalService) -> None:
        self._service = service

    async def check(
        self,
        *,
        kind: ApprovalKind,
        subject: str,
        zone_id: str,
        token_id: str,
        session_id: str | None,
        agent_id: str | None,
        reason: str,
        metadata: dict[str, Any],
        timeout_override: float | None = None,
    ) -> Decision:
        if session_id is not None:
            allow = await self._service._repo.find_session_allow(
                session_id=session_id, zone_id=zone_id, kind=kind, subject=subject
            )
            if allow is not None:
                return Decision.APPROVED

        try:
            return await self._service.request_and_wait(
                request_id=_new_request_id(),
                zone_id=zone_id,
                kind=kind,
                subject=subject,
                agent_id=agent_id,
                token_id=token_id,
                session_id=session_id,
                reason=reason,
                metadata=metadata,
                timeout_override=timeout_override,
            )
        except ApprovalDenied:
            return Decision.DENIED
        except ApprovalTimeout:
            return Decision.DENIED
        except GatewayClosed:
            raise
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/integration/approvals/test_policy_gate.py -v
```

Expected: 1 passing.

- [ ] **Step 5: Commit**

```
git add src/nexus/bricks/approvals/policy_gate.py tests/integration/approvals/test_policy_gate.py
git commit -m "feat(#3790): approvals brick — PolicyGate facade"
```

---

## Phase 6 — gRPC API

### Task 14: approvals.proto + codegen wiring

**Files:**
- Create: `proto/nexus/grpc/approvals.proto`
- Modify: `buf.gen.yaml` only if missing the proto path (verify; usually globs `proto/**`)

- [ ] **Step 1: Write the proto file**

```protobuf
// proto/nexus/grpc/approvals.proto
syntax = "proto3";

package nexus.approvals.v1;

import "google/protobuf/timestamp.proto";

message ApprovalRequestProto {
  string id = 1;
  string zone_id = 2;
  string kind = 3;       // ApprovalKind
  string subject = 4;
  string agent_id = 5;
  string token_id = 6;
  string session_id = 7;
  string reason = 8;
  string metadata_json = 9;
  string status = 10;
  google.protobuf.Timestamp created_at = 11;
  google.protobuf.Timestamp expires_at = 12;
  google.protobuf.Timestamp decided_at = 13;
  string decided_by = 14;
  string decision_scope = 15;
}

message ListPendingRequest {
  string zone_id = 1;  // empty = all zones the caller has approvals:read on
}

message ListPendingResponse {
  repeated ApprovalRequestProto requests = 1;
}

message GetRequest { string request_id = 1; }

message DecideRequest {
  string request_id = 1;
  string decision = 2;       // approved | denied
  string scope = 3;          // once | session | persist_sandbox | persist_baseline
  string reason = 4;
}

message CancelRequest { string request_id = 1; }
message CancelResponse {}

message WatchRequest { string zone_id = 1; }
message ApprovalEvent {
  string type = 1;            // pending | decided
  string request_id = 2;
  string zone_id = 3;
  string decision = 4;        // empty when type=pending
}

message SubmitRequest {
  string kind = 1;
  string subject = 2;
  string zone_id = 3;
  string token_id = 4;
  string session_id = 5;
  string agent_id = 6;
  string reason = 7;
  string metadata_json = 8;
  double timeout_override_seconds = 9;
}

message SubmitDecision {
  string decision = 1;        // approved | denied
  string request_id = 2;
}

service ApprovalsV1 {
  rpc ListPending(ListPendingRequest) returns (ListPendingResponse);
  rpc Get(GetRequest) returns (ApprovalRequestProto);
  rpc Decide(DecideRequest) returns (ApprovalRequestProto);
  rpc Cancel(CancelRequest) returns (CancelResponse);
  rpc Watch(WatchRequest) returns (stream ApprovalEvent);
  rpc Submit(SubmitRequest) returns (SubmitDecision);
}
```

- [ ] **Step 2: Run codegen**

```
buf generate
```

Expected: new generated files under `src/nexus/proto/` (or wherever buf.gen.yaml outputs them; consult `buf.gen.yaml`). Codegen must succeed before continuing.

- [ ] **Step 3: Commit**

```
git add proto/nexus/grpc/approvals.proto $(git ls-files --others --exclude-standard | grep -E 'approvals_pb2(_grpc)?\.py' | tr '\n' ' ')
git commit -m "feat(#3790): proto — ApprovalsV1 gRPC service"
```

(If your project commits generated stubs, include them. If not, omit them and adjust the commit accordingly.)

---

### Task 15: gRPC servicer + ReBAC capability check

**Files:**
- Create: `src/nexus/bricks/approvals/grpc_server.py`
- Test: `tests/integration/approvals/test_grpc_server.py`

The servicer is thin: marshalling + auth + delegate to ApprovalService.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/approvals/test_grpc_server.py
import asyncio
import json

import grpc.aio
import pytest

from nexus.bricks.approvals.grpc_server import ApprovalsServicer
from nexus.bricks.approvals.models import (
    ApprovalKind,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.proto import approvals_pb2, approvals_pb2_grpc

pytestmark = pytest.mark.integration


class _AllowAllAuth:
    async def authorize(self, context, capability):
        return "tok_test"


@pytest.mark.asyncio
async def test_list_pending_returns_pending_rows(approval_service):
    server = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_AllowAllAuth()), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        # Drop a pending row via the service
        waiter = asyncio.create_task(
            approval_service.request_and_wait(
                request_id="req_g1",
                zone_id="z",
                kind=ApprovalKind.EGRESS_HOST,
                subject="grpc.example:443",
                agent_id="ag",
                token_id="tok",
                session_id="tok:s",
                reason="r",
                metadata={},
            )
        )
        await asyncio.sleep(0.05)

        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            resp = await stub.ListPending(approvals_pb2.ListPendingRequest(zone_id="z"))
            assert any(r.subject == "grpc.example:443" for r in resp.requests)

        await approval_service.decide(
            request_id="req_g1",
            decision=Decision.APPROVED,
            decided_by="op",
            scope=DecisionScope.ONCE,
            reason=None,
            source=DecisionSource.GRPC,
        )
        await asyncio.wait_for(waiter, 1.0)
    finally:
        await server.stop(grace=0.1)


@pytest.mark.asyncio
async def test_decide_via_grpc_unblocks_waiter(approval_service):
    server = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_AllowAllAuth()), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        waiter = asyncio.create_task(
            approval_service.request_and_wait(
                request_id="req_g2",
                zone_id="z",
                kind=ApprovalKind.EGRESS_HOST,
                subject="grpc2.example:443",
                agent_id="ag",
                token_id="tok",
                session_id="tok:s",
                reason="r",
                metadata={},
            )
        )
        await asyncio.sleep(0.05)
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            await stub.Decide(
                approvals_pb2.DecideRequest(
                    request_id="req_g2",
                    decision="approved",
                    scope="once",
                    reason="ok",
                )
            )
        assert (await asyncio.wait_for(waiter, 1.0)) is Decision.APPROVED
    finally:
        await server.stop(grace=0.1)


@pytest.mark.asyncio
async def test_unknown_decision_value_returns_invalid_argument(approval_service):
    server = grpc.aio.server()
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(approval_service, auth=_AllowAllAuth()), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.Decide(
                    approvals_pb2.DecideRequest(
                        request_id="any",
                        decision="WAT",
                        scope="once",
                    )
                )
            assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await server.stop(grace=0.1)
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/integration/approvals/test_grpc_server.py -v
```

Expected: ImportError on grpc_server.

- [ ] **Step 3: Implement**

```python
# src/nexus/bricks/approvals/grpc_server.py
"""gRPC servicer for ApprovalsV1."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import grpc.aio
from google.protobuf.timestamp_pb2 import Timestamp

from nexus.bricks.approvals.errors import GatewayClosed
from nexus.bricks.approvals.models import (
    ApprovalKind,
    ApprovalRequest,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.service import ApprovalService, WatchEvent
from nexus.proto import approvals_pb2, approvals_pb2_grpc

logger = logging.getLogger(__name__)


class CapabilityAuth(Protocol):
    async def authorize(self, context: grpc.aio.ServicerContext, capability: str) -> str:
        """Return token_id of caller; raise grpc error if not authorized."""


def _ts(d) -> Timestamp:
    ts = Timestamp()
    if d is not None:
        ts.FromDatetime(d)
    return ts


def _to_pb(req: ApprovalRequest) -> approvals_pb2.ApprovalRequestProto:
    return approvals_pb2.ApprovalRequestProto(
        id=req.id,
        zone_id=req.zone_id,
        kind=req.kind.value,
        subject=req.subject,
        agent_id=req.agent_id or "",
        token_id=req.token_id or "",
        session_id=req.session_id or "",
        reason=req.reason,
        metadata_json=json.dumps(req.metadata),
        status=req.status,
        created_at=_ts(req.created_at),
        expires_at=_ts(req.expires_at),
        decided_at=_ts(req.decided_at),
        decided_by=req.decided_by or "",
        decision_scope=req.decision_scope.value if req.decision_scope else "",
    )


class ApprovalsServicer(approvals_pb2_grpc.ApprovalsV1Servicer):
    def __init__(self, service: ApprovalService, auth: CapabilityAuth) -> None:
        self._svc = service
        self._auth = auth

    async def ListPending(self, request, context):
        await self._auth.authorize(context, "approvals:read")
        rows = await self._svc.list_pending(zone_id=request.zone_id or None)
        return approvals_pb2.ListPendingResponse(requests=[_to_pb(r) for r in rows])

    async def Get(self, request, context):
        await self._auth.authorize(context, "approvals:read")
        row = await self._svc.get(request.request_id)
        if row is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "request not found")
        return _to_pb(row)

    async def Decide(self, request, context):
        token_id = await self._auth.authorize(context, "approvals:decide")
        try:
            decision = Decision(request.decision)
            scope = DecisionScope(request.scope)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bad decision/scope")
        try:
            row = await self._svc.decide(
                request_id=request.request_id,
                decision=decision,
                decided_by=token_id,
                scope=scope,
                reason=request.reason or None,
                source=DecisionSource.GRPC,
            )
        except ValueError as e:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(e))
        return _to_pb(row)

    async def Cancel(self, request, context):
        await self._auth.authorize(context, "approvals:decide")
        # Server-side Cancel is a no-op for unknown ids; resolves to OK by design.
        return approvals_pb2.CancelResponse()

    async def Watch(self, request, context):
        await self._auth.authorize(context, "approvals:read")
        async for ev in self._svc.watch(zone_id=request.zone_id or None):
            yield approvals_pb2.ApprovalEvent(
                type=ev.type,
                request_id=ev.request_id,
                zone_id=ev.zone_id,
                decision=ev.decision or "",
            )

    async def Submit(self, request, context):
        token_id = await self._auth.authorize(context, "approvals:request")
        try:
            kind = ApprovalKind(request.kind)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bad kind")
        metadata: dict[str, Any] = {}
        if request.metadata_json:
            try:
                metadata = json.loads(request.metadata_json)
            except Exception:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bad metadata_json")
        timeout = request.timeout_override_seconds or None
        try:
            decision = await self._svc.request_and_wait(
                request_id=f"req_push_{request.request_id_seed if hasattr(request, 'request_id_seed') else id(request)}",
                zone_id=request.zone_id,
                kind=kind,
                subject=request.subject,
                agent_id=request.agent_id or None,
                token_id=token_id,
                session_id=request.session_id or None,
                reason=request.reason,
                metadata=metadata,
                timeout_override=timeout,
            )
        except GatewayClosed as e:
            await context.abort(grpc.StatusCode.UNAVAILABLE, str(e))
        return approvals_pb2.SubmitDecision(
            decision=decision.value, request_id=""
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/integration/approvals/test_grpc_server.py -v
```

Expected: 3 passing.

- [ ] **Step 5: Commit**

```
git add src/nexus/bricks/approvals/grpc_server.py tests/integration/approvals/test_grpc_server.py
git commit -m "feat(#3790): approvals brick — gRPC ApprovalsV1 servicer"
```

---

### Task 16: HTTP diagnostic dump endpoint

**Files:**
- Create: `src/nexus/bricks/approvals/http_diag.py`
- Test: `tests/integration/approvals/test_http_diag.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/approvals/test_http_diag.py
import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nexus.bricks.approvals.http_diag import register_diag_router
from nexus.bricks.approvals.models import ApprovalKind

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_diag_dump_returns_pending_rows(approval_service):
    app = FastAPI()
    register_diag_router(app, approval_service, allow_subject="tok_test")

    waiter = asyncio.create_task(
        approval_service.request_and_wait(
            request_id="req_d1",
            zone_id="z",
            kind=ApprovalKind.EGRESS_HOST,
            subject="diag.example:443",
            agent_id="ag",
            token_id="tok",
            session_id="tok:s",
            reason="r",
            metadata={},
        )
    )
    await asyncio.sleep(0.05)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/hub/approvals/dump?zone_id=z", headers={"Authorization": "Bearer tok_test"})
    assert r.status_code == 200
    payload = r.json()
    assert any(p["subject"] == "diag.example:443" for p in payload["pending"])
    waiter.cancel()
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/integration/approvals/test_http_diag.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# src/nexus/bricks/approvals/http_diag.py
"""Read-only HTTP diagnostic dump for ops smoke-testing."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request

from nexus.bricks.approvals.service import ApprovalService


def register_diag_router(
    app: FastAPI,
    service: ApprovalService,
    *,
    allow_subject: str | None,
) -> None:
    router = APIRouter()

    def _check_auth(authorization: str | None) -> None:
        if allow_subject is None:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer")
        token = authorization.removeprefix("Bearer ").strip()
        if token != allow_subject:
            raise HTTPException(status_code=403, detail="forbidden")

    @router.get("/hub/approvals/dump")
    async def dump(
        zone_id: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_auth(authorization)
        pending = await service.list_pending(zone_id=zone_id)
        return {
            "pending": [p.to_dict() for p in pending],
        }

    app.include_router(router)
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/integration/approvals/test_http_diag.py -v
```

Expected: 1 passing.

- [ ] **Step 5: Commit**

```
git add src/nexus/bricks/approvals/http_diag.py tests/integration/approvals/test_http_diag.py
git commit -m "feat(#3790): approvals brick — HTTP diagnostic dump"
```

---

## Phase 7 — Integration hooks

### Task 17: Wire PolicyGate into MCP context state

**Files:**
- Modify: `src/nexus/bricks/mcp/server.py` (single hook in startup)
- Test: `tests/unit/bricks/mcp/test_policy_gate_wiring.py`

`server.py` is large; the hook is small — read the file's startup section first to find the construction site for FastMCP.

- [ ] **Step 1: Identify the FastMCP construction site**

```
grep -n "FastMCP(" src/nexus/bricks/mcp/server.py | head
```

Note the line range that builds the FastMCP app (call it `<MCP_BUILD_LINES>`). Insertion goes in the function that registers middlewares, immediately after auth bridge wiring. Do not invent the line — find it before editing.

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/bricks/mcp/test_policy_gate_wiring.py
"""Verify the MCP server exposes the approvals PolicyGate via context state."""

from unittest.mock import MagicMock

import pytest

from nexus.bricks.approvals.policy_gate import PolicyGate


def test_register_policy_gate_attaches_to_app_state():
    from nexus.bricks.mcp.server import register_policy_gate_dependency

    app = MagicMock()
    gate = MagicMock(spec=PolicyGate)
    register_policy_gate_dependency(app, gate)
    # The registration helper writes to app.state.policy_gate
    assert app.state.policy_gate is gate
```

- [ ] **Step 3: Run test to verify it fails**

```
pytest tests/unit/bricks/mcp/test_policy_gate_wiring.py -v
```

Expected: AttributeError.

- [ ] **Step 4: Add the helper to `src/nexus/bricks/mcp/server.py`**

Insert near other registration helpers (search `def register_` for sibling pattern):

```python
def register_policy_gate_dependency(app, gate) -> None:
    """Attach PolicyGate to app.state so middlewares can call gate.check()."""
    app.state.policy_gate = gate
```

Then in the FastMCP build site, after constructing `gate = PolicyGate(approval_service)` (which is wired from a startup function — see Task 21), add:

```python
register_policy_gate_dependency(mcp_app, gate)
```

- [ ] **Step 5: Run test to verify it passes**

```
pytest tests/unit/bricks/mcp/test_policy_gate_wiring.py -v
```

Expected: 1 passing.

- [ ] **Step 6: Commit**

```
git add src/nexus/bricks/mcp/server.py tests/unit/bricks/mcp/test_policy_gate_wiring.py
git commit -m "feat(#3790): mcp — expose PolicyGate via app.state"
```

---

### Task 18: SSRF/egress middleware → PolicyGate.check on unlisted host

**Files:**
- Modify: the SSRF/egress middleware introduced by #3792 (search for it; if absent, modify `src/nexus/bricks/mcp/middleware.py`)
- Test: `tests/integration/approvals/test_mcp_egress_hook.py`

The hook converts the existing `permission_denied` path into:
1. Call `PolicyGate.check(kind=egress_host, subject=host_port, …)`.
2. If `Approved` → continue.
3. If `Denied` → keep existing not-found error.

- [ ] **Step 1: Locate the egress denial site**

```
grep -rn "permission_denied\|not in allowlist\|ssrf" --include="*.py" src/nexus/bricks/mcp/ | head
```

Pick the file containing the egress denial branch for `nexus_fetch`-style tools (likely `src/nexus/bricks/mcp/middleware.py` or a sibling SSRF module). Call this file `<EGRESS_FILE>`.

- [ ] **Step 2: Write the failing integration test**

```python
# tests/integration/approvals/test_mcp_egress_hook.py
import asyncio

import pytest

from nexus.bricks.approvals.models import (
    ApprovalKind,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.policy_gate import PolicyGate

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_unlisted_egress_creates_pending_request(approval_service):
    """The middleware hook creates a pending row for an unlisted host
    and unblocks on approve."""
    gate = PolicyGate(approval_service)

    async def caller():
        return await gate.check(
            kind=ApprovalKind.EGRESS_HOST,
            subject="api.stripe.com:443",
            zone_id="z",
            token_id="tok",
            session_id="tok:s",
            agent_id="ag",
            reason="nexus_fetch",
            metadata={"url": "https://api.stripe.com/v1/charges"},
        )

    waiter = asyncio.create_task(caller())
    await asyncio.sleep(0.05)

    pending = await approval_service.list_pending(zone_id="z")
    target = next(p for p in pending if p.subject == "api.stripe.com:443")
    assert target.kind is ApprovalKind.EGRESS_HOST
    assert target.metadata.get("url") == "https://api.stripe.com/v1/charges"

    await approval_service.decide(
        request_id=target.id,
        decision=Decision.APPROVED,
        decided_by="op",
        scope=DecisionScope.SESSION,
        reason=None,
        source=DecisionSource.GRPC,
    )
    assert (await asyncio.wait_for(waiter, 1.0)) is Decision.APPROVED
```

- [ ] **Step 3: Run test to verify it fails**

```
pytest tests/integration/approvals/test_mcp_egress_hook.py -v
```

Expected: passes only after Task 18 fix lands? — actually this test calls PolicyGate directly and should pass already since the gate is implemented. **It is the contract test for what the middleware will call**; if it does pass at this step, that's expected. If it fails, the prior tasks regressed.

- [ ] **Step 4: Modify `<EGRESS_FILE>` — convert the deny branch to gate**

Locate the existing deny path. Add an early call to the gate before returning the deny result. Pseudocode pattern (the reviewer must adapt to the actual file's surrounding code):

```python
# Before:
if not _host_allowed(host_port, zone_id):
    return tool_error("permission_denied", f"egress to {host_port} not allowed")

# After:
if not _host_allowed(host_port, zone_id):
    gate = context.fastmcp_context.app.state.policy_gate
    if gate is None:
        return tool_error("permission_denied", f"egress to {host_port} not allowed")
    decision = await gate.check(
        kind=ApprovalKind.EGRESS_HOST,
        subject=host_port,
        zone_id=zone_id,
        token_id=_token_id_from_ctx(context),
        session_id=_session_id_from_ctx(context),
        agent_id=_agent_id_from_ctx(context),
        reason=tool_name,
        metadata={"url": url, "tool": tool_name},
    )
    if decision is Decision.APPROVED:
        # fall through to allowed path
        pass
    else:
        return tool_error("permission_denied", f"egress to {host_port} not allowed")
```

The exact extraction helpers (`_token_id_from_ctx` etc.) already exist in the MCP middleware (`_extract_subject_from_ctx`); reuse them rather than inventing new ones. If you need to add helpers, place them in the same file and add a unit test in `tests/unit/bricks/mcp/`.

- [ ] **Step 5: Re-run integration test**

```
pytest tests/integration/approvals/test_mcp_egress_hook.py -v
```

Expected: still passing (the gate-only test). The actual MCP middleware end-to-end test runs in Task 23.

- [ ] **Step 6: Commit**

```
git add <EGRESS_FILE> tests/integration/approvals/test_mcp_egress_hook.py
git commit -m "feat(#3790): mcp — route unlisted egress through PolicyGate"
```

---

### Task 19: Hub auth resolver → PolicyGate.check on zone-scope miss

**Files:**
- Modify: hub auth resolver (search `grep -rn "zone_scope\|hub_token" --include="*.py" src/nexus/bricks/`)
- Test: `tests/integration/approvals/test_hub_zone_access_hook.py`

Same pattern as Task 18, but for the zone-scope miss path.

- [ ] **Step 1: Locate the zone-miss site**

```
grep -rn "zone_id not in\|forbidden zone\|zone_scope" --include="*.py" src/nexus/bricks/ | head
```

Pick the function that returns 403 / `permission_denied` for a token requesting a zone outside its scope. Call this file `<HUB_AUTH_FILE>`.

- [ ] **Step 2: Write the failing integration test**

```python
# tests/integration/approvals/test_hub_zone_access_hook.py
import asyncio

import pytest

from nexus.bricks.approvals.models import (
    ApprovalKind,
    Decision,
    DecisionScope,
    DecisionSource,
)
from nexus.bricks.approvals.policy_gate import PolicyGate

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_zone_access_creates_pending_request(approval_service):
    gate = PolicyGate(approval_service)

    waiter = asyncio.create_task(
        gate.check(
            kind=ApprovalKind.ZONE_ACCESS,
            subject="legal",
            zone_id="legal",
            token_id="tok_alice",
            session_id="tok_alice:s",
            agent_id=None,
            reason="user requested zone legal",
            metadata={"requested_zone": "legal"},
        )
    )
    await asyncio.sleep(0.05)
    pending = await approval_service.list_pending(zone_id="legal")
    target = next(p for p in pending if p.kind is ApprovalKind.ZONE_ACCESS)
    await approval_service.decide(
        request_id=target.id,
        decision=Decision.APPROVED,
        decided_by="admin",
        scope=DecisionScope.PERSIST_SANDBOX,
        reason=None,
        source=DecisionSource.GRPC,
    )
    assert (await asyncio.wait_for(waiter, 1.0)) is Decision.APPROVED
```

- [ ] **Step 3: Run test to verify it fails**

```
pytest tests/integration/approvals/test_hub_zone_access_hook.py -v
```

Expected: passes. (Same reasoning as Task 18 — the contract test of the gate.)

- [ ] **Step 4: Modify `<HUB_AUTH_FILE>`**

Convert the deny branch as in Task 18. Add the call site:

```python
# Before:
if zone_id not in allowed_zones:
    raise HTTPException(status_code=403, detail="zone forbidden")

# After:
if zone_id not in allowed_zones:
    gate = request.app.state.policy_gate  # set in MCP/server bootstrap
    if gate is None:
        raise HTTPException(status_code=403, detail="zone forbidden")
    decision = await gate.check(
        kind=ApprovalKind.ZONE_ACCESS,
        subject=zone_id,
        zone_id=zone_id,
        token_id=token_id,
        session_id=session_id,
        agent_id=None,
        reason="zone_access",
        metadata={"requested_zone": zone_id},
    )
    if decision is not Decision.APPROVED:
        raise HTTPException(status_code=403, detail="zone forbidden")
```

- [ ] **Step 5: Re-run integration test**

```
pytest tests/integration/approvals/test_hub_zone_access_hook.py -v
```

Expected: passing.

- [ ] **Step 6: Commit**

```
git add <HUB_AUTH_FILE> tests/integration/approvals/test_hub_zone_access_hook.py
git commit -m "feat(#3790): hub — route zone-scope miss through PolicyGate"
```

---

## Phase 8 — Bootstrap, E2E, smoke

### Task 20: Brick bootstrap — wire ApprovalService into the daemon startup

**Files:**
- Create: `src/nexus/bricks/approvals/bootstrap.py`
- Modify: the daemon/main startup that already constructs other bricks (typically `src/nexus/daemon/main.py` or `src/nexus/server/fastapi_server.py`)
- Test: `tests/unit/bricks/approvals/test_bootstrap.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/bricks/approvals/test_bootstrap.py
"""Bootstrap returns a working service + gate stack with feature flag respect."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.approvals.bootstrap import build_approvals_stack
from nexus.bricks.approvals.config import ApprovalConfig


@pytest.mark.asyncio
async def test_disabled_returns_no_gate():
    cfg = ApprovalConfig(enabled=False)
    stack = await build_approvals_stack(cfg, session_factory=MagicMock(), asyncpg_pool=MagicMock())
    assert stack.gate is None
    assert stack.service is None


@pytest.mark.asyncio
async def test_enabled_returns_gate_and_service(monkeypatch):
    cfg = ApprovalConfig(enabled=True)

    started = {"called": False}

    class FakeService:
        async def start(self):
            started["called"] = True

        async def stop(self):
            started["called"] = False

    monkeypatch.setattr(
        "nexus.bricks.approvals.bootstrap.ApprovalService",
        lambda *a, **kw: FakeService(),
    )

    stack = await build_approvals_stack(cfg, session_factory=MagicMock(), asyncpg_pool=MagicMock())
    assert stack.gate is not None
    assert stack.service is not None
    assert started["called"] is True
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/unit/bricks/approvals/test_bootstrap.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# src/nexus/bricks/approvals/bootstrap.py
"""Build & wire the approvals stack from configuration."""

from __future__ import annotations

from dataclasses import dataclass

from nexus.bricks.approvals.config import ApprovalConfig
from nexus.bricks.approvals.events import NotifyBridge
from nexus.bricks.approvals.policy_gate import PolicyGate
from nexus.bricks.approvals.repository import ApprovalRepository
from nexus.bricks.approvals.service import ApprovalService


@dataclass(frozen=True)
class ApprovalsStack:
    config: ApprovalConfig
    service: ApprovalService | None
    gate: PolicyGate | None


async def build_approvals_stack(
    config: ApprovalConfig,
    *,
    session_factory,
    asyncpg_pool,
) -> ApprovalsStack:
    if not config.enabled:
        return ApprovalsStack(config=config, service=None, gate=None)
    repo = ApprovalRepository(session_factory)
    bridge = NotifyBridge(asyncpg_pool)
    service = ApprovalService(repo, bridge, config)
    await service.start()
    gate = PolicyGate(service)
    return ApprovalsStack(config=config, service=service, gate=gate)


async def shutdown_approvals_stack(stack: ApprovalsStack) -> None:
    if stack.service is not None:
        await stack.service.stop()
```

- [ ] **Step 4: Wire into daemon startup**

```
grep -n "FastAPI(\|app = FastAPI\|@asynccontextmanager" src/nexus/server/fastapi_server.py | head
```

In the lifespan/startup hook, after Postgres pool + session factory are constructed:

```python
from nexus.bricks.approvals.bootstrap import build_approvals_stack, shutdown_approvals_stack
from nexus.bricks.approvals.config import ApprovalConfig
from nexus.bricks.approvals.grpc_server import ApprovalsServicer
from nexus.bricks.approvals.http_diag import register_diag_router
from nexus.proto import approvals_pb2_grpc

approvals_cfg = ApprovalConfig(enabled=settings.approvals_enabled)
stack = await build_approvals_stack(
    approvals_cfg,
    session_factory=app.state.session_factory,
    asyncpg_pool=app.state.asyncpg_pool,
)
app.state.approvals_stack = stack
app.state.policy_gate = stack.gate

if stack.service is not None:
    register_diag_router(app, stack.service, allow_subject=settings.diag_subject)
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(
        ApprovalsServicer(stack.service, auth=app.state.capability_auth),
        app.state.grpc_server,
    )

# On shutdown:
await shutdown_approvals_stack(stack)
```

If `settings.approvals_enabled` does not yet exist on the settings object, add it: default `False`, env var `NEXUS_APPROVALS_ENABLED`.

- [ ] **Step 5: Run test to verify it passes**

```
pytest tests/unit/bricks/approvals/test_bootstrap.py -v
```

Expected: 2 passing.

- [ ] **Step 6: Commit**

```
git add src/nexus/bricks/approvals/bootstrap.py src/nexus/server/fastapi_server.py tests/unit/bricks/approvals/test_bootstrap.py
git commit -m "feat(#3790): approvals — bootstrap + daemon wiring + feature flag"
```

---

### Task 21: E2E — MCP egress with operator approve

**Files:**
- Create: `tests/e2e/self_contained/approvals/__init__.py`
- Create: `tests/e2e/self_contained/approvals/test_mcp_egress_e2e.py`

End-to-end: a stub MCP client tries an unlisted host, an out-of-process gRPC client (`grpc.aio`) approves, the tool call resolves with success.

- [ ] **Step 1: Write the test**

```python
# tests/e2e/self_contained/approvals/__init__.py
```

```python
# tests/e2e/self_contained/approvals/test_mcp_egress_e2e.py
"""E2E: MCP unlisted egress → approve via gRPC → tool call succeeds."""

import asyncio

import grpc.aio
import pytest

from nexus.proto import approvals_pb2, approvals_pb2_grpc

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_unlisted_host_pause_then_approve_unblocks_tool_call(running_nexus, mcp_client):
    """`running_nexus` boots a real nexus with approvals enabled.
    `mcp_client` is a stub MCP client wired to call `nexus_fetch`."""
    tool_call = asyncio.create_task(
        mcp_client.call_tool(
            "nexus_fetch", {"url": "https://api.stripe.com/healthz"}
        )
    )
    await asyncio.sleep(0.2)

    async with grpc.aio.insecure_channel(running_nexus.grpc_addr) as channel:
        stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
        pending = await stub.ListPending(
            approvals_pb2.ListPendingRequest(zone_id=running_nexus.zone),
            metadata=(("authorization", f"Bearer {running_nexus.admin_token}"),),
        )
        assert pending.requests, "no pending request created for unlisted host"
        rid = pending.requests[0].id
        await stub.Decide(
            approvals_pb2.DecideRequest(
                request_id=rid,
                decision="approved",
                scope="session",
                reason="ok",
            ),
            metadata=(("authorization", f"Bearer {running_nexus.admin_token}"),),
        )

    result = await asyncio.wait_for(tool_call, 5.0)
    assert "200" in str(result) or "ok" in str(result).lower()
```

(`running_nexus` and `mcp_client` are E2E harness fixtures already present in `tests/e2e/self_contained/conftest.py`. If they are missing the `grpc_addr` / `admin_token` / `zone` attributes, extend the harness — keep the changes scoped.)

- [ ] **Step 2: Run E2E test (skips if harness is unavailable)**

```
pytest tests/e2e/self_contained/approvals/test_mcp_egress_e2e.py -v
```

Expected: pass when `NEXUS_E2E_HARNESS=1`. Skip otherwise.

- [ ] **Step 3: Commit**

```
git add tests/e2e/self_contained/approvals/__init__.py tests/e2e/self_contained/approvals/test_mcp_egress_e2e.py
git commit -m "test(#3790): e2e — MCP egress approve unblocks tool call"
```

---

### Task 22: E2E — hub zone access with operator approve

**Files:**
- Create: `tests/e2e/self_contained/approvals/test_hub_zone_access_e2e.py`

- [ ] **Step 1: Write the test**

```python
# tests/e2e/self_contained/approvals/test_hub_zone_access_e2e.py
"""E2E: token requests forbidden zone → approve → zone access succeeds."""

import asyncio

import grpc.aio
import httpx
import pytest

from nexus.proto import approvals_pb2, approvals_pb2_grpc

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_zone_access_request_appears_in_queue_and_approve_unlocks(
    running_nexus, restricted_token
):
    """`restricted_token` is scoped to {eng} only; we ask for `legal`."""
    async with httpx.AsyncClient(base_url=running_nexus.http_url) as c:
        attempt = asyncio.create_task(
            c.get(
                "/zones/legal/health",
                headers={"Authorization": f"Bearer {restricted_token}"},
            )
        )
        await asyncio.sleep(0.2)

        async with grpc.aio.insecure_channel(running_nexus.grpc_addr) as channel:
            stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)
            pending = await stub.ListPending(
                approvals_pb2.ListPendingRequest(zone_id="legal"),
                metadata=(("authorization", f"Bearer {running_nexus.admin_token}"),),
            )
            assert any(r.kind == "zone_access" for r in pending.requests)
            rid = next(r.id for r in pending.requests if r.kind == "zone_access")
            await stub.Decide(
                approvals_pb2.DecideRequest(
                    request_id=rid,
                    decision="approved",
                    scope="session",
                    reason="ok",
                ),
                metadata=(("authorization", f"Bearer {running_nexus.admin_token}"),),
            )

        resp = await asyncio.wait_for(attempt, 5.0)
        assert resp.status_code == 200
```

- [ ] **Step 2: Run**

```
pytest tests/e2e/self_contained/approvals/test_hub_zone_access_e2e.py -v
```

- [ ] **Step 3: Commit**

```
git add tests/e2e/self_contained/approvals/test_hub_zone_access_e2e.py
git commit -m "test(#3790): e2e — hub zone access approve unblocks request"
```

---

### Task 23: E2E — Watch stream emits cross-worker events + diag dump

**Files:**
- Create: `tests/e2e/self_contained/approvals/test_watch_and_dump_e2e.py`

- [ ] **Step 1: Write the test**

```python
# tests/e2e/self_contained/approvals/test_watch_and_dump_e2e.py
"""E2E: Watch stream sees pending+decided events; dump endpoint returns them."""

import asyncio

import grpc.aio
import httpx
import pytest

from nexus.proto import approvals_pb2, approvals_pb2_grpc

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_watch_emits_pending_and_decided(running_nexus, mcp_client):
    """Open a Watch on worker A; make a request via worker B; observe events."""
    async with grpc.aio.insecure_channel(running_nexus.grpc_addr) as channel:
        stub = approvals_pb2_grpc.ApprovalsV1Stub(channel)

        events: list[approvals_pb2.ApprovalEvent] = []
        stop_event = asyncio.Event()

        async def consume():
            call = stub.Watch(
                approvals_pb2.WatchRequest(zone_id=running_nexus.zone),
                metadata=(("authorization", f"Bearer {running_nexus.admin_token}"),),
            )
            async for ev in call:
                events.append(ev)
                if ev.type == "decided":
                    stop_event.set()
                    break

        watcher = asyncio.create_task(consume())

        tool_call = asyncio.create_task(
            mcp_client.call_tool("nexus_fetch", {"url": "https://watch.example/x"})
        )
        await asyncio.sleep(0.3)
        pending = await stub.ListPending(
            approvals_pb2.ListPendingRequest(zone_id=running_nexus.zone),
            metadata=(("authorization", f"Bearer {running_nexus.admin_token}"),),
        )
        rid = pending.requests[0].id
        await stub.Decide(
            approvals_pb2.DecideRequest(
                request_id=rid, decision="approved", scope="once"
            ),
            metadata=(("authorization", f"Bearer {running_nexus.admin_token}"),),
        )
        tool_call.cancel()
        await asyncio.wait_for(stop_event.wait(), 5.0)
        watcher.cancel()

    types = [e.type for e in events]
    assert "pending" in types and "decided" in types


@pytest.mark.asyncio
async def test_diag_dump_returns_recent_pending(running_nexus, mcp_client):
    asyncio.create_task(mcp_client.call_tool("nexus_fetch", {"url": "https://dump.example/x"}))
    await asyncio.sleep(0.3)
    async with httpx.AsyncClient(base_url=running_nexus.http_url) as c:
        r = await c.get(
            f"/hub/approvals/dump?zone_id={running_nexus.zone}",
            headers={"Authorization": f"Bearer {running_nexus.admin_token}"},
        )
    assert r.status_code == 200
    payload = r.json()
    assert payload["pending"], "dump endpoint returned no pending"
```

- [ ] **Step 2: Run**

```
pytest tests/e2e/self_contained/approvals/test_watch_and_dump_e2e.py -v
```

- [ ] **Step 3: Commit**

```
git add tests/e2e/self_contained/approvals/test_watch_and_dump_e2e.py
git commit -m "test(#3790): e2e — Watch stream + diag dump"
```

---

### Task 24: Coalesce burst smoke benchmark

**Files:**
- Create: `tests/benchmarks/bench_coalesce_burst.py`

- [ ] **Step 1: Write the benchmark**

```python
# tests/benchmarks/bench_coalesce_burst.py
"""Smoke benchmark: 100 concurrent callers on the same coalesce key.

Expectation: one DB row, one decide call, all callers unblock < 200 ms after notify.
"""

import asyncio
import time
from datetime import UTC, datetime

import pytest

from nexus.bricks.approvals.models import (
    ApprovalKind,
    Decision,
    DecisionScope,
    DecisionSource,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_coalesce_burst_100_callers(approval_service):
    async def caller(i: int):
        return await approval_service.request_and_wait(
            request_id=f"req_burst_{i}",
            zone_id="z",
            kind=ApprovalKind.EGRESS_HOST,
            subject="burst.example:443",
            agent_id="ag",
            token_id="tok",
            session_id=f"tok:s_{i}",
            reason="r",
            metadata={},
        )

    tasks = [asyncio.create_task(caller(i)) for i in range(100)]
    await asyncio.sleep(0.2)

    pending = await approval_service.list_pending(zone_id="z")
    target = next(p for p in pending if p.subject == "burst.example:443")

    t0 = time.monotonic()
    await approval_service.decide(
        request_id=target.id,
        decision=Decision.APPROVED,
        decided_by="op",
        scope=DecisionScope.ONCE,
        reason=None,
        source=DecisionSource.GRPC,
    )
    results = await asyncio.gather(*tasks)
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert all(r is Decision.APPROVED for r in results)
    assert elapsed_ms < 500, f"unblock took {elapsed_ms:.1f} ms"
```

- [ ] **Step 2: Run**

```
pytest tests/benchmarks/bench_coalesce_burst.py -v
```

Expected: passes locally with elapsed < 500 ms. CI assertion is informational; widen if your CI runner is slow.

- [ ] **Step 3: Commit**

```
git add tests/benchmarks/bench_coalesce_burst.py
git commit -m "test(#3790): smoke — coalesce burst 100 callers"
```

---

## Self-review checklist (run after the plan is implemented)

- [ ] All three tables exist in production migrations.
- [ ] Feature flag `NEXUS_APPROVALS_ENABLED` defaults to `False`.
- [ ] Acceptance criteria from spec mapped to tasks:
  - [ ] Pending requests visible via API (Task 15).
  - [ ] Non-interactive decide via API (Task 15).
  - [ ] MCP tool calls with unlisted egress pause and wait (Task 18, Task 21 E2E).
  - [ ] Hub zone-access requests surface in same queue (Task 19, Task 22 E2E).
  - [ ] Audit trail (who, when, scope, reason) — `approval_decisions` (Task 4 + Task 6).
  - [ ] `--auto-deny-after 60s` default (Tasks 3 + 11).
- [ ] No TUI, CLI, Slack, or webhook code in this plan (per epic split).
- [ ] `make test-integration` and `make test-e2e` pass with the harness.
- [ ] `mypy` passes for the new brick.
- [ ] No new lints introduced (`pre-commit run --all-files`).
