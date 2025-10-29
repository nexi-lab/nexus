"""add_sql_views_for_ready_work_detection

Revision ID: 278a3d730040
Revises: 9c0780bb05c1
Create Date: 2025-10-17 00:12:19.968467

Adds SQL views for efficient work detection:
- ready_work_items: Files with status='ready' and no blockers
- pending_work_items: Files with status='pending' ordered by priority
- blocked_work_items: Files blocked by dependencies
- work_by_priority: All work items ordered by priority
- in_progress_work: Files currently being processed
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "278a3d730040"
down_revision: Union[str, Sequence[str], None] = "9c0780bb05c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create SQL views for work detection."""
    # Import views after op is available
    from nexus.storage import views

    # Detect database type from connection
    connection = op.get_bind()
    db_type = "sqlite"
    if connection.dialect.name in ("postgresql", "postgres"):
        db_type = "postgresql"

    # Create all views with appropriate SQL for this database
    all_views = views.get_all_views(db_type)
    for _name, view_sql in all_views:
        connection.execute(view_sql)
        connection.commit()


def downgrade() -> None:
    """Drop SQL views."""
    from nexus.storage import views

    # Drop all views
    connection = op.get_bind()
    for drop_sql in views.DROP_VIEWS:
        connection.execute(drop_sql)
        connection.commit()
