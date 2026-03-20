"""Concrete GitHub CLI connector class.

CLIConnector subclass for GitHub operations via the ``gh`` CLI.
Instantiate directly or via the declarative YAML config.

Phase 6 (Issue #3148).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nexus.backends.base.registry import register_connector
from nexus.backends.connectors.base import (
    ConfirmLevel,
    ErrorDef,
    OpTraits,
    Reversibility,
)
from nexus.backends.connectors.cli.base import CLIConnector
from nexus.backends.connectors.cli.config import CLIConnectorConfig
from nexus.backends.connectors.github.schemas import (
    CloseIssueSchema,
    CommentIssueSchema,
    CreateIssueSchema,
    CreatePRSchema,
    MergePRSchema,
)

logger = logging.getLogger(__name__)


@register_connector(
    "gws_github",
    description="GitHub via gh CLI",
    category="cli",
    service_name="github",
)
class GitHubConnector(CLIConnector):
    """GitHub CLI connector via ``gh``."""

    SKILL_NAME = "github"
    CLI_NAME = "gh"
    CLI_SERVICE = ""  # gh has no service subcommand — commands are "gh issue create", not "gh github issue create"

    SCHEMAS: dict[str, type] = {
        "create_issue": CreateIssueSchema,
        "create_pr": CreatePRSchema,
        "comment_issue": CommentIssueSchema,
        "close_issue": CloseIssueSchema,
        "merge_pr": MergePRSchema,
    }
    OPERATION_TRAITS: dict[str, OpTraits] = {
        "create_issue": OpTraits(reversibility=Reversibility.FULL, confirm=ConfirmLevel.INTENT),
        "create_pr": OpTraits(reversibility=Reversibility.FULL, confirm=ConfirmLevel.EXPLICIT),
        "comment_issue": OpTraits(reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.INTENT),
        "close_issue": OpTraits(reversibility=Reversibility.FULL, confirm=ConfirmLevel.EXPLICIT),
        "merge_pr": OpTraits(
            reversibility=Reversibility.NONE,
            confirm=ConfirmLevel.USER,
            warnings=["THIS ACTION CANNOT BE UNDONE — the PR will be merged."],
        ),
    }
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
        ),
        "ISSUE_NOT_FOUND": ErrorDef(
            message="Issue or PR not found",
            skill_section="operations",
            fix_example="number: <valid issue or PR number>",
        ),
        "PR_NOT_MERGEABLE": ErrorDef(
            message="PR cannot be merged (conflicts or checks failing)",
            skill_section="operations",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = self._load_config()
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)

    @staticmethod
    def _load_config() -> CLIConnectorConfig | None:
        config_path = Path(__file__).parent / "config.yaml"
        if config_path.exists():
            from nexus.backends.connectors.cli.loader import load_connector_config

            return load_connector_config(config_path)
        return None
