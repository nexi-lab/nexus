"""Declarative connector configuration model.

Pydantic models for validating CLI connector YAML configs at load time.
Errors surface immediately with file path and field references, not at
runtime when operations are first attempted.

Design decisions (Issue #3148):
    - Pydantic for config validation (12A) — dogfoods the project's own patterns
    - Exhaustive field validation: missing fields, invalid refs, duplicates
    - Version field for forward-compatible config evolution
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class AuthConfig(BaseModel):
    """Authentication configuration for a CLI connector."""

    provider: str = Field(
        ...,
        description="OAuth provider name (e.g., 'google', 'github')",
    )
    flag: str = Field(
        default="--access-token",
        description="CLI flag for passing auth token (stdin-piped, not arg)",
    )
    scopes: list[str] = Field(
        default_factory=list,
        description="OAuth scopes required by this connector",
    )


class WriteOperationConfig(BaseModel):
    """Configuration for a single write operation."""

    path: str = Field(
        ...,
        description="Magic path pattern (e.g., 'SENT/_new.yaml')",
    )
    operation: str = Field(
        ...,
        description="Operation name (e.g., 'send_email')",
    )
    schema_ref: str = Field(
        ...,
        description="Dotted path to Pydantic schema class",
    )
    command: str = Field(
        ...,
        description="CLI command to execute (e.g., '+send')",
    )
    traits: dict[str, str] = Field(
        default_factory=dict,
        description="Operation traits: reversibility, confirm level",
    )

    @model_validator(mode="after")
    def _validate_traits(self) -> "WriteOperationConfig":
        valid_reversibility = {"full", "partial", "none"}
        valid_confirm = {"none", "intent", "explicit", "user"}
        if (
            "reversibility" in self.traits
            and self.traits["reversibility"] not in valid_reversibility
        ):
            msg = (
                f"Invalid reversibility '{self.traits['reversibility']}', "
                f"must be one of {valid_reversibility}"
            )
            raise ValueError(msg)
        if "confirm" in self.traits and self.traits["confirm"] not in valid_confirm:
            msg = (
                f"Invalid confirm level '{self.traits['confirm']}', must be one of {valid_confirm}"
            )
            raise ValueError(msg)
        return self


class ReadConfig(BaseModel):
    """Configuration for read operations."""

    list_command: str = Field(
        ...,
        description="CLI command for listing items",
    )
    get_command: str = Field(
        ...,
        description="CLI command for getting a single item",
    )
    format: Literal["yaml", "json", "text"] = Field(
        default="yaml",
        description="Output format from CLI",
    )


class SyncConfig(BaseModel):
    """Configuration for delta sync."""

    delta_command: str = Field(
        ...,
        description="CLI command for delta listing (e.g., 'messages.list --after {since}')",
    )
    watch_command: str | None = Field(
        default=None,
        description="CLI command for real-time watch (optional)",
    )
    state_field: str = Field(
        default="state_token",
        description="Field name in CLI output that contains the state token",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Items per sync page",
    )


class SkillsConfig(BaseModel):
    """Configuration for skill doc generation."""

    import_from_cli: bool = Field(
        default=False,
        description="Import skill definitions from CLI's own SKILL.md files",
    )
    schema_docs: bool = Field(
        default=True,
        description="Generate schema documentation from Pydantic models",
    )


class CLIConnectorConfig(BaseModel):
    """Root configuration for a declarative CLI connector.

    Validates the complete YAML config at load time. Invalid configs
    fail immediately with clear error messages.

    Example YAML::

        version: 1
        connector:
          type: cli
          cli: gws
          service: gmail
          auth:
            provider: google
            flag: "--access-token"
          read:
            list_command: "messages.list"
            get_command: "messages.get"
            format: yaml
          write:
            - path: "SENT/_new.yaml"
              operation: send_email
              schema: nexus.backends.connectors.gmail.schemas.SendEmailSchema
              command: "+send"
              traits: {reversibility: none, confirm: user}
          sync:
            delta_command: "messages.list --after {since}"
            state_field: historyId
    """

    version: int = Field(
        default=1,
        ge=1,
        le=1,
        description="Config format version (currently only version 1)",
    )
    type: Literal["cli"] = Field(
        default="cli",
        description="Connector type (currently only 'cli')",
    )
    cli: str = Field(
        ...,
        min_length=1,
        description="CLI binary name (e.g., 'gws', 'gh')",
    )
    service: str = Field(
        ...,
        min_length=1,
        description="Service name within the CLI (e.g., 'gmail', 'issue')",
    )
    auth: AuthConfig = Field(
        ...,
        description="Authentication configuration",
    )
    read: ReadConfig | None = Field(
        default=None,
        description="Read operation configuration",
    )
    write: list[WriteOperationConfig] = Field(
        default_factory=list,
        description="Write operation configurations",
    )
    sync: SyncConfig | None = Field(
        default=None,
        description="Delta sync configuration",
    )
    skills: SkillsConfig = Field(
        default_factory=SkillsConfig,
        description="Skill documentation configuration",
    )
    error_patterns: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Custom error patterns for CLIErrorMapper",
    )

    @model_validator(mode="after")
    def _validate_unique_operations(self) -> "CLIConnectorConfig":
        """Ensure no duplicate operation names or write paths."""
        seen_ops: set[str] = set()
        seen_paths: set[str] = set()
        for write_op in self.write:
            if write_op.operation in seen_ops:
                msg = f"Duplicate operation name: '{write_op.operation}'"
                raise ValueError(msg)
            seen_ops.add(write_op.operation)
            if write_op.path in seen_paths:
                msg = f"Duplicate write path: '{write_op.path}'"
                raise ValueError(msg)
            seen_paths.add(write_op.path)
        return self
