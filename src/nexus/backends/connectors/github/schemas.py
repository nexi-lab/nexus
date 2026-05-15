"""Pydantic schemas for GitHub CLI connector operations.

Phase 6 (Issue #3148).
"""

from pydantic import BaseModel, Field


class CreateIssueSchema(BaseModel):
    """Create a GitHub issue."""

    agent_intent: str = Field(..., min_length=10, description="Why this issue is being created")
    title: str = Field(..., min_length=1, max_length=256, description="Issue title")
    body: str = Field(default="", description="Issue body in markdown")
    labels: list[str] = Field(default_factory=list, description="Labels to apply")
    assignees: list[str] = Field(default_factory=list, description="GitHub usernames to assign")
    milestone: str | None = Field(default=None, description="Milestone title or number")
    confirm: bool = Field(default=False, description="Explicit confirmation")


class CreatePRSchema(BaseModel):
    """Create a GitHub pull request."""

    agent_intent: str = Field(..., min_length=10, description="Why this PR is being created")
    title: str = Field(..., min_length=1, max_length=256, description="PR title")
    body: str = Field(default="", description="PR description in markdown")
    base: str = Field(default="main", description="Base branch")
    head: str = Field(..., description="Head branch with changes")
    labels: list[str] = Field(default_factory=list, description="Labels to apply")
    reviewers: list[str] = Field(default_factory=list, description="Reviewer usernames")
    draft: bool = Field(default=False, description="Create as draft PR")
    confirm: bool = Field(default=False, description="Explicit confirmation")


class CommentIssueSchema(BaseModel):
    """Add a comment to an issue or PR."""

    agent_intent: str = Field(..., min_length=10, description="Why this comment is being added")
    number: int = Field(..., ge=1, description="Issue or PR number")
    body: str = Field(..., min_length=1, description="Comment body in markdown")
    confirm: bool = Field(default=False, description="Explicit confirmation")


class CloseIssueSchema(BaseModel):
    """Close a GitHub issue."""

    agent_intent: str = Field(..., min_length=10, description="Why this issue is being closed")
    number: int = Field(..., ge=1, description="Issue number to close")
    reason: str = Field(default="completed", description="Close reason: completed or not_planned")
    comment: str | None = Field(default=None, description="Optional closing comment")
    user_confirmed: bool = Field(default=False, description="User confirmed closing")


class MergePRSchema(BaseModel):
    """Merge a pull request."""

    agent_intent: str = Field(..., min_length=10, description="Why this PR is being merged")
    number: int = Field(..., ge=1, description="PR number to merge")
    method: str = Field(default="squash", description="Merge method: merge, squash, or rebase")
    delete_branch: bool = Field(default=True, description="Delete head branch after merge")
    user_confirmed: bool = Field(default=False, description="User confirmed merge (irreversible)")
