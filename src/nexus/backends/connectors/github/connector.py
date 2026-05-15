"""Concrete GitHub CLI connector class.

PathCLIBackend subclass for GitHub operations via the ``gh`` CLI.
Instantiate directly or via the declarative YAML config.

Phase 6 (Issue #3148).
Human-readable display paths added in Issue #3256.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.backends.base.cli_backend import PathCLIBackend, sanitize_filename
from nexus.backends.base.registry import register_connector
from nexus.backends.connectors.base import (
    ConfirmLevel,
    ErrorDef,
    OpTraits,
    Reversibility,
)
from nexus.backends.connectors.base_errors import TRAIT_ERRORS
from nexus.backends.connectors.cli.config import CLIConnectorConfig
from nexus.backends.connectors.github.schemas import (
    CloseIssueSchema,
    CommentIssueSchema,
    CreateIssueSchema,
    CreatePRSchema,
    MergePRSchema,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


@register_connector("github_connector")
@register_connector("gws_github")
class GitHubConnector(PathCLIBackend):
    """GitHub CLI connector via ``gh``."""

    SKILL_NAME = "github"
    CLI_NAME = "gh"
    CLI_SERVICE = ""  # gh has no service subcommand

    DIRECTORY_STRUCTURE = """\
/mnt/github/
  issues/
    {number}_{title}.yaml          # Issue as YAML (title, body, state, labels)
    _new.yaml                      # ✏ Write here to CREATE an issue
    _comment.yaml                  # ✏ Write here to COMMENT on an issue
    _close.yaml                    # ✏ Write here to CLOSE an issue
  pulls/
    {number}_{title}.yaml          # PR as YAML (title, body, state, reviews)
    _new.yaml                      # ✏ Write here to CREATE a pull request
    _merge.yaml                    # ✏ Write here to MERGE a PR (⚠ irreversible)
  .readme/
    README.md"""

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
        **TRAIT_ERRORS,
        "ISSUE_NOT_FOUND": ErrorDef(
            message="Issue or PR not found",
            readme_section="operations",
            fix_example="number: <valid issue or PR number>",
        ),
        "PR_NOT_MERGEABLE": ErrorDef(
            message="PR cannot be merged (conflicts or checks failing)",
            readme_section="operations",
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

    def raw_read(self, path: str, context: Any = None) -> bytes:
        """Read raw repository content through ``gh api``.

        Path shape: ``owner/repo/ref/path/to/file``. For refs containing
        slashes, candidate ref/path boundaries are retried until one succeeds.
        """
        parts = path.strip("/").split("/")
        if len(parts) < 4:
            return self.read_content(path, context=context)

        owner, repo, *rest = parts
        if not owner or not repo:
            return self.read_content(path, context=context)

        token = self._get_user_token(context)
        auth_env = self._build_auth_env(token) if token else None
        attempted = False
        for split_at in range(len(rest) - 1, 0, -1):
            ref = "/".join(rest[:split_at])
            file_path = "/".join(rest[split_at:])
            if not ref or not file_path:
                continue
            attempted = True
            args = [
                self.CLI_NAME,
                "api",
                f"repos/{owner}/{repo}/contents/{file_path}?ref={ref}",
                "-H",
                "Accept: application/vnd.github.raw",
            ]
            result = self._execute_cli(args, context=context, env=auth_env)
            if result.ok:
                return result.stdout.encode("utf-8")
        return b"" if attempted else self.read_content(path, context=context)

    def display_path(self, item_id: str, metadata: dict[str, Any] | None = None) -> str:
        """Generate human-readable path for GitHub issues/PRs.

        Format: ``issues/{number}_{title}.yaml`` or ``pulls/{number}_{title}.yaml``
        Example: ``issues/142_feat-add-grove-status-command.yaml``
        """
        meta = metadata or {}

        # Determine subfolder from item type.
        item_type = meta.get("type", meta.get("pull_request"))
        if item_type == "PullRequest" or meta.get("pull_request") is not None:
            folder = "pulls"
        else:
            folder = "issues"

        number = meta.get("number", "")
        title = meta.get("title", "")

        if number and title:
            safe_title = sanitize_filename(title, max_len=80)
            return f"{folder}/{number}_{safe_title}.yaml"
        elif number:
            return f"{folder}/{number}.yaml"

        return f"{folder}/{item_id}.yaml"

    def fingerprint(self, path: str, context: "OperationContext | None" = None) -> str | None:
        parent, _, name = path.rpartition("/")
        metadata = self.list_dir_metadata(parent or "/", context=context) or {}
        row = metadata.get(name) or {}
        candidate = row.get("sha") or row.get("blob_sha") or row.get("etag")
        return str(candidate) if candidate else None
