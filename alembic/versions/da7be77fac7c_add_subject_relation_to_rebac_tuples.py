"""add_subject_relation_to_rebac_tuples

Revision ID: da7be77fac7c
Revises: b98c750d8d1a
Create Date: 2025-10-25 09:42:07.486468

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "da7be77fac7c"
down_revision: Union[str, Sequence[str], None] = "b98c750d8d1a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add subject_relation column to rebac_tuples for userset-as-subject support."""
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("rebac_tuples")}

    # Column may already exist if rebac_tuples was created with it included
    if "subject_relation" not in columns:
        op.add_column("rebac_tuples", sa.Column("subject_relation", sa.String(50), nullable=True))

    # Add index for subject sets (subject_type, subject_id, subject_relation)
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("rebac_tuples")}
    if "idx_rebac_subject_relation" not in existing_indexes:
        op.create_index(
            "idx_rebac_subject_relation",
            "rebac_tuples",
            ["subject_type", "subject_id", "subject_relation"],
        )


def downgrade() -> None:
    """Remove subject_relation column from rebac_tuples."""
    from contextlib import suppress

    from sqlalchemy.exc import OperationalError, ProgrammingError

    with suppress(OperationalError, ProgrammingError):
        op.drop_index("idx_rebac_subject_relation", table_name="rebac_tuples")
    with suppress(OperationalError, ProgrammingError):
        op.drop_column("rebac_tuples", "subject_relation")
