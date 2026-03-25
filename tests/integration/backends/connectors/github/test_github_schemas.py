"""Tests for GitHub CLI connector schemas and config (Phase 6, Issue #3148).

Covers:
- Valid data accepted for all five schemas
- Missing required fields rejected
- Too-short / invalid field values rejected
- Defaults applied correctly
- YAML config loads via load_connector_config
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus.backends.connectors.cli.loader import load_connector_config
from nexus.backends.connectors.github.schemas import (
    CloseIssueSchema,
    CommentIssueSchema,
    CreateIssueSchema,
    CreatePRSchema,
    MergePRSchema,
)

# Path to the real config.yaml shipped with the connector
GITHUB_CONFIG_PATH = (
    Path(__file__).resolve().parents[5]
    / "src"
    / "nexus"
    / "backends"
    / "connectors"
    / "github"
    / "config.yaml"
)


# ---------------------------------------------------------------------------
# CreateIssueSchema
# ---------------------------------------------------------------------------


class TestCreateIssueSchema:
    def test_valid_minimal(self) -> None:
        schema = CreateIssueSchema(
            agent_intent="Tracking a bug reported by the user in chat",
            title="Fix login page crash",
        )
        assert schema.title == "Fix login page crash"
        assert schema.body == ""
        assert schema.labels == []
        assert schema.assignees == []
        assert schema.milestone is None
        assert schema.confirm is False

    def test_valid_full(self) -> None:
        schema = CreateIssueSchema(
            agent_intent="User asked to file a feature request for dark mode",
            title="Add dark mode support",
            body="## Description\nUsers want dark mode.",
            labels=["enhancement", "ui"],
            assignees=["alice", "bob"],
            milestone="v2.0",
            confirm=True,
        )
        assert schema.labels == ["enhancement", "ui"]
        assert schema.assignees == ["alice", "bob"]
        assert schema.milestone == "v2.0"
        assert schema.confirm is True

    def test_missing_agent_intent(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateIssueSchema.model_validate({"title": "Some title"})
        assert "agent_intent" in str(exc_info.value)

    def test_missing_title(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateIssueSchema.model_validate({"agent_intent": "This is a valid intent for testing"})
        assert "title" in str(exc_info.value)

    def test_agent_intent_too_short(self) -> None:
        with pytest.raises(ValidationError):
            CreateIssueSchema(
                agent_intent="short",
                title="Some title",
            )

    def test_title_empty(self) -> None:
        with pytest.raises(ValidationError):
            CreateIssueSchema(
                agent_intent="This is a valid intent for testing",
                title="",
            )

    def test_title_too_long(self) -> None:
        with pytest.raises(ValidationError):
            CreateIssueSchema(
                agent_intent="This is a valid intent for testing",
                title="x" * 257,
            )


# ---------------------------------------------------------------------------
# CreatePRSchema
# ---------------------------------------------------------------------------


class TestCreatePRSchema:
    def test_valid_minimal(self) -> None:
        schema = CreatePRSchema(
            agent_intent="Submitting the feature branch for review",
            title="Add dark mode support",
            head="feature/dark-mode",
        )
        assert schema.base == "main"
        assert schema.head == "feature/dark-mode"
        assert schema.draft is False
        assert schema.confirm is False
        assert schema.labels == []
        assert schema.reviewers == []

    def test_valid_full(self) -> None:
        schema = CreatePRSchema(
            agent_intent="Submitting the feature branch for review by team",
            title="Add dark mode support",
            body="## Changes\n- Added dark mode toggle",
            base="develop",
            head="feature/dark-mode",
            labels=["enhancement"],
            reviewers=["alice"],
            draft=True,
            confirm=True,
        )
        assert schema.base == "develop"
        assert schema.draft is True
        assert schema.reviewers == ["alice"]

    def test_missing_head(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreatePRSchema.model_validate(
                {
                    "agent_intent": "Submitting the feature branch for review",
                    "title": "Some PR title",
                }
            )
        assert "head" in str(exc_info.value)

    def test_missing_title(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreatePRSchema.model_validate(
                {
                    "agent_intent": "Submitting the feature branch for review",
                    "head": "feature/x",
                }
            )
        assert "title" in str(exc_info.value)

    def test_agent_intent_too_short(self) -> None:
        with pytest.raises(ValidationError):
            CreatePRSchema(
                agent_intent="short",
                title="Some title",
                head="feature/x",
            )

    def test_title_too_long(self) -> None:
        with pytest.raises(ValidationError):
            CreatePRSchema(
                agent_intent="This is a valid intent for testing",
                title="x" * 257,
                head="feature/x",
            )


# ---------------------------------------------------------------------------
# CommentIssueSchema
# ---------------------------------------------------------------------------


class TestCommentIssueSchema:
    def test_valid(self) -> None:
        schema = CommentIssueSchema(
            agent_intent="Adding status update as requested by user",
            number=42,
            body="This is now resolved in v2.1.",
        )
        assert schema.number == 42
        assert schema.confirm is False

    def test_missing_number(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CommentIssueSchema.model_validate(
                {
                    "agent_intent": "Adding status update as requested by user",
                    "body": "Some comment",
                }
            )
        assert "number" in str(exc_info.value)

    def test_missing_body(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CommentIssueSchema.model_validate(
                {
                    "agent_intent": "Adding status update as requested by user",
                    "number": 42,
                }
            )
        assert "body" in str(exc_info.value)

    def test_number_zero(self) -> None:
        with pytest.raises(ValidationError):
            CommentIssueSchema(
                agent_intent="Adding status update as requested by user",
                number=0,
                body="Some comment",
            )

    def test_number_negative(self) -> None:
        with pytest.raises(ValidationError):
            CommentIssueSchema(
                agent_intent="Adding status update as requested by user",
                number=-1,
                body="Some comment",
            )

    def test_body_empty(self) -> None:
        with pytest.raises(ValidationError):
            CommentIssueSchema(
                agent_intent="Adding status update as requested by user",
                number=42,
                body="",
            )

    def test_agent_intent_too_short(self) -> None:
        with pytest.raises(ValidationError):
            CommentIssueSchema(
                agent_intent="short",
                number=42,
                body="Some comment",
            )


# ---------------------------------------------------------------------------
# CloseIssueSchema
# ---------------------------------------------------------------------------


class TestCloseIssueSchema:
    def test_valid_minimal(self) -> None:
        schema = CloseIssueSchema(
            agent_intent="Issue was resolved by the latest deployment",
            number=99,
        )
        assert schema.number == 99
        assert schema.reason == "completed"
        assert schema.comment is None
        assert schema.user_confirmed is False

    def test_valid_full(self) -> None:
        schema = CloseIssueSchema(
            agent_intent="Closing as not planned per team decision",
            number=99,
            reason="not_planned",
            comment="Decided not to pursue this direction.",
            user_confirmed=True,
        )
        assert schema.reason == "not_planned"
        assert schema.comment == "Decided not to pursue this direction."
        assert schema.user_confirmed is True

    def test_missing_number(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CloseIssueSchema.model_validate(
                {"agent_intent": "Issue was resolved by the latest deployment"}
            )
        assert "number" in str(exc_info.value)

    def test_number_zero(self) -> None:
        with pytest.raises(ValidationError):
            CloseIssueSchema(
                agent_intent="Issue was resolved by the latest deployment",
                number=0,
            )

    def test_agent_intent_too_short(self) -> None:
        with pytest.raises(ValidationError):
            CloseIssueSchema(
                agent_intent="short",
                number=99,
            )


# ---------------------------------------------------------------------------
# MergePRSchema
# ---------------------------------------------------------------------------


class TestMergePRSchema:
    def test_valid_minimal(self) -> None:
        schema = MergePRSchema(
            agent_intent="All checks passed and review approved",
            number=123,
        )
        assert schema.number == 123
        assert schema.method == "squash"
        assert schema.delete_branch is True
        assert schema.user_confirmed is False

    def test_valid_full(self) -> None:
        schema = MergePRSchema(
            agent_intent="All checks passed and review approved by team",
            number=123,
            method="rebase",
            delete_branch=False,
            user_confirmed=True,
        )
        assert schema.method == "rebase"
        assert schema.delete_branch is False
        assert schema.user_confirmed is True

    def test_missing_number(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            MergePRSchema.model_validate({"agent_intent": "All checks passed and review approved"})
        assert "number" in str(exc_info.value)

    def test_number_zero(self) -> None:
        with pytest.raises(ValidationError):
            MergePRSchema(
                agent_intent="All checks passed and review approved",
                number=0,
            )

    def test_number_negative(self) -> None:
        with pytest.raises(ValidationError):
            MergePRSchema(
                agent_intent="All checks passed and review approved",
                number=-5,
            )

    def test_agent_intent_too_short(self) -> None:
        with pytest.raises(ValidationError):
            MergePRSchema(
                agent_intent="short",
                number=123,
            )


# ---------------------------------------------------------------------------
# YAML config loads via load_connector_config
# ---------------------------------------------------------------------------


class TestGitHubConfigYAML:
    def test_config_loads_successfully(self) -> None:
        """The shipped config.yaml validates against CLIConnectorConfig."""
        config = load_connector_config(GITHUB_CONFIG_PATH)
        assert config.cli == "gh"
        assert config.service == "github"
        assert config.auth.provider == "github"

    def test_config_read_section(self) -> None:
        config = load_connector_config(GITHUB_CONFIG_PATH)
        assert config.read is not None
        assert config.read.list_command == "issue list"
        assert config.read.get_command == "issue view"
        assert config.read.format == "json"

    def test_config_write_operations(self) -> None:
        config = load_connector_config(GITHUB_CONFIG_PATH)
        assert len(config.write) == 5
        op_names = [op.operation for op in config.write]
        assert "create_issue" in op_names
        assert "create_pr" in op_names
        assert "comment_issue" in op_names
        assert "close_issue" in op_names
        assert "merge_pr" in op_names

    def test_config_write_traits(self) -> None:
        config = load_connector_config(GITHUB_CONFIG_PATH)
        ops = {op.operation: op for op in config.write}

        assert ops["create_issue"].traits["reversibility"] == "full"
        assert ops["create_issue"].traits["confirm"] == "intent"

        assert ops["merge_pr"].traits["reversibility"] == "none"
        assert ops["merge_pr"].traits["confirm"] == "user"

        assert ops["create_pr"].traits["confirm"] == "explicit"

    def test_config_write_schema_refs(self) -> None:
        config = load_connector_config(GITHUB_CONFIG_PATH)
        ops = {op.operation: op for op in config.write}

        assert (
            ops["create_issue"].schema_ref
            == "nexus.backends.connectors.github.schemas.CreateIssueSchema"
        )
        assert (
            ops["merge_pr"].schema_ref == "nexus.backends.connectors.github.schemas.MergePRSchema"
        )

    def test_config_sync_section(self) -> None:
        config = load_connector_config(GITHUB_CONFIG_PATH)
        assert config.sync is not None
        assert "issue list" in config.sync.delta_command
        assert config.sync.state_field == "updatedAt"
        assert config.sync.page_size == 100

    def test_config_unique_paths(self) -> None:
        """All write paths are unique (enforced by CLIConnectorConfig validator)."""
        config = load_connector_config(GITHUB_CONFIG_PATH)
        paths = [op.path for op in config.write]
        assert len(paths) == len(set(paths))

    def test_config_unique_operations(self) -> None:
        """All operation names are unique (enforced by CLIConnectorConfig validator)."""
        config = load_connector_config(GITHUB_CONFIG_PATH)
        ops = [op.operation for op in config.write]
        assert len(ops) == len(set(ops))


# ---------------------------------------------------------------------------
# GitHubConnector command construction
# ---------------------------------------------------------------------------


class TestGitHubConnectorCommands:
    """Verify GitHubConnector builds correct gh CLI invocations."""

    def test_cli_service_is_empty(self) -> None:
        """gh has no service subcommand — CLI_SERVICE must be empty."""
        from nexus.backends.connectors.github.connector import GitHubConnector

        assert GitHubConnector.CLI_SERVICE == ""

    def test_build_cli_args_no_service_prefix(self) -> None:
        """gh commands should be ['gh', 'issue', 'create'], NOT ['gh', 'github', 'issue create']."""
        from unittest.mock import MagicMock

        from nexus.backends.connectors.github.connector import GitHubConnector

        connector = GitHubConnector()
        args = connector._build_cli_args("create_issue", MagicMock(), "issues/_new.yaml")

        # Must start with 'gh', must NOT include 'github' as second element
        assert args[0] == "gh"
        assert "github" not in args
        # 'issue create' must be split into separate args
        assert "issue" in args
        assert "create" in args

    def test_build_cli_args_merge_pr(self) -> None:
        """'pr merge' command must be split into ['pr', 'merge']."""
        from unittest.mock import MagicMock

        from nexus.backends.connectors.github.connector import GitHubConnector

        connector = GitHubConnector()
        args = connector._build_cli_args("merge_pr", MagicMock(), "pulls/_merge.yaml")

        assert args == ["gh", "pr", "merge"]


# ---------------------------------------------------------------------------
# Display path tests (Issue #3256)
# ---------------------------------------------------------------------------


class TestGitHubDisplayPath:
    """Test GitHubConnector.display_path() for human-readable issue/PR paths."""

    def _connector(self):
        from nexus.backends.connectors.github.connector import GitHubConnector

        return GitHubConnector.__new__(GitHubConnector)

    def test_issue_with_number_and_title(self) -> None:
        c = self._connector()
        path = c.display_path(
            "issue-142",
            {
                "number": 142,
                "title": "feat: add grove status command",
            },
        )
        assert path == "issues/142_feat-add-grove-status-command.yaml"

    def test_pr_detected_by_type(self) -> None:
        c = self._connector()
        path = c.display_path(
            "pr-99",
            {
                "number": 99,
                "title": "fix auth bug",
                "type": "PullRequest",
            },
        )
        assert path.startswith("pulls/")
        assert "99" in path
        assert "fix-auth-bug" in path

    def test_pr_detected_by_pull_request_key(self) -> None:
        c = self._connector()
        path = c.display_path(
            "pr-50",
            {
                "number": 50,
                "title": "Update docs",
                "pull_request": {"url": "https://..."},
            },
        )
        assert path.startswith("pulls/")

    def test_no_title_uses_number(self) -> None:
        c = self._connector()
        path = c.display_path("issue-10", {"number": 10})
        assert path == "issues/10.yaml"

    def test_no_metadata_uses_id(self) -> None:
        c = self._connector()
        path = c.display_path("abc123", None)
        assert path == "issues/abc123.yaml"

    def test_special_chars_in_title_sanitized(self) -> None:
        c = self._connector()
        path = c.display_path(
            "issue-1",
            {
                "number": 1,
                "title": 'feat: "quoted" path/name?',
            },
        )
        assert "?" not in path
        assert '"' not in path
