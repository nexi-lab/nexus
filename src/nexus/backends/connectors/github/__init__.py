"""GitHub CLI connector schemas.

This module provides Pydantic schemas for GitHub operations via the ``gh`` CLI:
- CreateIssueSchema: For creating new issues
- CreatePRSchema: For creating pull requests
- CommentIssueSchema: For commenting on issues/PRs
- CloseIssueSchema: For closing issues
- MergePRSchema: For merging pull requests

Phase 6 (Issue #3148).
"""

from nexus.backends.connectors.github.schemas import (
    CloseIssueSchema,
    CommentIssueSchema,
    CreateIssueSchema,
    CreatePRSchema,
    MergePRSchema,
)

__all__ = [
    "CloseIssueSchema",
    "CommentIssueSchema",
    "CreateIssueSchema",
    "CreatePRSchema",
    "MergePRSchema",
]
