"""Merge add_posix_uid and tiger_resource_map_remove_tenant

Revision ID: merge_posix_tiger
Revises: add_posix_uid, tiger_resource_map_remove_tenant
Create Date: 2026-01-01

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "merge_posix_tiger"
down_revision: Union[str, Sequence[str], None] = (
    "add_posix_uid",
    "tiger_resource_map_remove_tenant",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
