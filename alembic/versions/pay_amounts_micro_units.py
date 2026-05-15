"""Convert Nexus Pay SQL amounts from cents to micro-credits.

Revision ID: pay_amounts_micro_units
Revises: add_chunks_embedding_halfvec
Create Date: 2026-05-07

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "pay_amounts_micro_units"
down_revision: Union[str, Sequence[str], None] = "add_chunks_embedding_halfvec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PAY_AMOUNT_TABLES = ("payment_transaction_meta", "credit_reservation_meta")
CENT_TO_MICRO_FACTOR = 10_000


def _existing_tables(bind: sa.engine.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade_pay_amounts(bind: sa.engine.Connection) -> None:
    tables = _existing_tables(bind)
    for table_name in PAY_AMOUNT_TABLES:
        if table_name in tables:
            bind.execute(
                sa.text(f"UPDATE {table_name} SET amount = amount * :factor"),
                {"factor": CENT_TO_MICRO_FACTOR},
            )


def downgrade_pay_amounts(bind: sa.engine.Connection) -> None:
    tables = _existing_tables(bind)
    for table_name in PAY_AMOUNT_TABLES:
        if table_name in tables:
            bind.execute(
                sa.text(f"UPDATE {table_name} SET amount = amount / :factor"),
                {"factor": CENT_TO_MICRO_FACTOR},
            )


def upgrade() -> None:
    upgrade_pay_amounts(op.get_bind())


def downgrade() -> None:
    downgrade_pay_amounts(op.get_bind())
