"""SQLAlchemy model for the scheduler task queue.

Replaces runtime DDL in server/lifespan/services.py and raw asyncpg SQL
in scheduler/queue.py with a proper ORM model.

Related: Issue #1212
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Numeric, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models._base import Base, uuid_pk


class ScheduledTaskModel(Base):
    """Persistent storage for scheduled tasks with priority ordering."""

    __tablename__ = "scheduled_tasks"

    id: Mapped[str] = uuid_pk()
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    executor_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    priority_tier: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)
    effective_tier: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    boost_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
    boost_tiers: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    boost_reservation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default=ROOT_ZONE_ID)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "idx_scheduled_tasks_dequeue",
            "effective_tier",
            "enqueued_at",
            postgresql_where="status = 'queued'",
        ),
        Index("idx_scheduled_tasks_status", "status"),
        Index("idx_scheduled_tasks_zone", "zone_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<ScheduledTask id={self.id!r} type={self.task_type!r} "
            f"status={self.status!r} tier={self.effective_tier}>"
        )
