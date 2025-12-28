"""Update file namespace with shared-* relations for cross-tenant sharing

Revision ID: update_file_namespace_shared
Revises: None
Create Date: 2025-12-27

This migration updates the 'file' namespace to include shared-viewer, shared-editor,
and shared-owner relations which are required for cross-tenant sharing to work.

The issue was that existing databases had the file namespace created before these
relations were added to DEFAULT_FILE_NAMESPACE, so they were missing.
"""

from collections.abc import Sequence
from typing import Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "update_file_namespace_shared"
down_revision: Union[str, None] = None
branch_labels: Sequence[str] | None = ("file_namespace_shared",)
depends_on: Union[str, Sequence[str], None] = None


# The correct file namespace config with shared-* relations
FILE_NAMESPACE_CONFIG = """{
    "relations": {
        "parent": {},
        "direct_owner": {},
        "direct_editor": {},
        "direct_viewer": {},
        "parent_owner": {"tupleToUserset": {"tupleset": "parent", "computedUserset": "owner"}},
        "parent_editor": {"tupleToUserset": {"tupleset": "parent", "computedUserset": "editor"}},
        "parent_viewer": {"tupleToUserset": {"tupleset": "parent", "computedUserset": "viewer"}},
        "group_owner": {"tupleToUserset": {"tupleset": "direct_owner", "computedUserset": "member"}},
        "group_editor": {"tupleToUserset": {"tupleset": "direct_editor", "computedUserset": "member"}},
        "group_viewer": {"tupleToUserset": {"tupleset": "direct_viewer", "computedUserset": "member"}},
        "shared-viewer": {},
        "shared-editor": {},
        "shared-owner": {},
        "owner": {"union": ["direct_owner", "parent_owner", "group_owner", "shared-owner"]},
        "editor": {"union": ["direct_editor", "parent_editor", "group_editor", "shared-editor", "shared-owner"]},
        "viewer": {"union": ["direct_viewer", "parent_viewer", "group_viewer", "shared-viewer", "shared-editor", "shared-owner"]}
    },
    "permissions": {
        "read": ["editor", "viewer", "owner"],
        "write": ["editor", "owner"],
        "execute": ["owner"]
    }
}"""


def upgrade() -> None:
    """Update file namespace to include shared-* relations."""
    # Update the file namespace config
    op.execute(
        text(
            """
            UPDATE rebac_namespaces
            SET config = :config, updated_at = CURRENT_TIMESTAMP
            WHERE object_type = 'file'
            """
        ).bindparams(config=FILE_NAMESPACE_CONFIG)
    )


def downgrade() -> None:
    """Remove shared-* relations from file namespace (not recommended)."""
    # Note: This will break cross-tenant sharing functionality
    old_config = """{
        "relations": {
            "parent": {},
            "direct_owner": {},
            "direct_editor": {},
            "direct_viewer": {},
            "parent_owner": {"tupleToUserset": {"tupleset": "parent", "computedUserset": "owner"}},
            "parent_editor": {"tupleToUserset": {"tupleset": "parent", "computedUserset": "editor"}},
            "parent_viewer": {"tupleToUserset": {"tupleset": "parent", "computedUserset": "viewer"}},
            "group_owner": {"tupleToUserset": {"tupleset": "direct_owner", "computedUserset": "member"}},
            "group_editor": {"tupleToUserset": {"tupleset": "direct_editor", "computedUserset": "member"}},
            "group_viewer": {"tupleToUserset": {"tupleset": "direct_viewer", "computedUserset": "member"}},
            "owner": {"union": ["direct_owner", "parent_owner", "group_owner"]},
            "editor": {"union": ["direct_editor", "parent_editor", "group_editor"]},
            "viewer": {"union": ["direct_viewer", "parent_viewer", "group_viewer"]}
        },
        "permissions": {
            "read": ["viewer", "editor", "owner"],
            "write": ["editor", "owner"],
            "execute": ["owner"]
        }
    }"""
    op.execute(
        text(
            """
            UPDATE rebac_namespaces
            SET config = :config, updated_at = CURRENT_TIMESTAMP
            WHERE object_type = 'file'
            """
        ).bindparams(config=old_config)
    )
