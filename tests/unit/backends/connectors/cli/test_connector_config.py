"""Tests for CLIConnectorConfig Pydantic validation (Issue #3148, Decision #12A).

Covers:
- Valid config loading
- Missing required fields
- Invalid schema references
- Duplicate operation names and write paths
- Invalid trait values
- Version validation
- Edge cases: empty write list, missing optional sections
"""

import pytest
from pydantic import ValidationError

from nexus.backends.connectors.cli.config import (
    AuthConfig,
    CLIConnectorConfig,
    ReadConfig,
    SyncConfig,
    WriteOperationConfig,
)

# ---------------------------------------------------------------------------
# Valid configs
# ---------------------------------------------------------------------------


class TestValidConfig:
    def test_minimal_config(self) -> None:
        """Minimal valid config: CLI + service + auth, no read/write/sync."""
        config = CLIConnectorConfig(
            cli="gws",
            service="gmail",
            auth=AuthConfig(provider="google"),
        )
        assert config.cli == "gws"
        assert config.service == "gmail"
        assert config.version == 1
        assert config.write == []
        assert config.read is None
        assert config.sync is None

    def test_full_config(self) -> None:
        """Complete config with all sections populated."""
        config = CLIConnectorConfig(
            version=1,
            cli="gws",
            service="gmail",
            auth=AuthConfig(
                provider="google",
                flag="--access-token",
                scopes=["gmail.send", "gmail.readonly"],
            ),
            read=ReadConfig(
                list_command="messages.list",
                get_command="messages.get",
                format="yaml",
            ),
            write=[
                WriteOperationConfig(
                    path="SENT/_new.yaml",
                    operation="send_email",
                    schema_ref="nexus.connectors.gmail.schemas.SendEmailSchema",
                    command="+send",
                    traits={"reversibility": "none", "confirm": "user"},
                ),
                WriteOperationConfig(
                    path="DRAFTS/_new.yaml",
                    operation="create_draft",
                    schema_ref="nexus.connectors.gmail.schemas.DraftSchema",
                    command="drafts.create",
                    traits={"reversibility": "full", "confirm": "intent"},
                ),
            ],
            sync=SyncConfig(
                delta_command="messages.list --after {since}",
                state_field="historyId",
                page_size=50,
            ),
        )
        assert len(config.write) == 2
        assert config.sync is not None
        assert config.sync.page_size == 50

    def test_defaults_applied(self) -> None:
        config = CLIConnectorConfig(
            cli="gh",
            service="issue",
            auth=AuthConfig(provider="github"),
        )
        assert config.type == "cli"
        assert config.version == 1
        assert config.skills.schema_docs is True
        assert config.skills.import_from_cli is False
        assert config.error_patterns == []


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------


class TestMissingFields:
    def test_missing_cli(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CLIConnectorConfig.model_validate({"service": "gmail", "auth": {"provider": "google"}})
        assert "cli" in str(exc_info.value)

    def test_missing_service(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CLIConnectorConfig.model_validate({"cli": "gws", "auth": {"provider": "google"}})
        assert "service" in str(exc_info.value)

    def test_missing_auth(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CLIConnectorConfig.model_validate({"cli": "gws", "service": "gmail"})
        assert "auth" in str(exc_info.value)

    def test_missing_auth_provider(self) -> None:
        with pytest.raises(ValidationError):
            CLIConnectorConfig.model_validate({"cli": "gws", "service": "gmail", "auth": {}})

    def test_empty_cli_name(self) -> None:
        with pytest.raises(ValidationError):
            CLIConnectorConfig(
                cli="",
                service="gmail",
                auth=AuthConfig(provider="google"),
            )

    def test_empty_service_name(self) -> None:
        with pytest.raises(ValidationError):
            CLIConnectorConfig(
                cli="gws",
                service="",
                auth=AuthConfig(provider="google"),
            )


# ---------------------------------------------------------------------------
# Write operation validation
# ---------------------------------------------------------------------------


class TestWriteOperationValidation:
    def test_missing_write_op_path(self) -> None:
        with pytest.raises(ValidationError):
            WriteOperationConfig.model_validate(
                {"operation": "send", "schema_ref": "foo.Schema", "command": "+send"}
            )

    def test_missing_write_op_schema_ref(self) -> None:
        with pytest.raises(ValidationError):
            WriteOperationConfig.model_validate(
                {"path": "SENT/_new.yaml", "operation": "send", "command": "+send"}
            )

    def test_invalid_reversibility_trait(self) -> None:
        with pytest.raises(ValidationError, match="reversibility"):
            WriteOperationConfig(
                path="SENT/_new.yaml",
                operation="send",
                schema_ref="foo.Schema",
                command="+send",
                traits={"reversibility": "maybe"},
            )

    def test_invalid_confirm_trait(self) -> None:
        with pytest.raises(ValidationError, match="confirm"):
            WriteOperationConfig(
                path="SENT/_new.yaml",
                operation="send",
                schema_ref="foo.Schema",
                command="+send",
                traits={"confirm": "sometimes"},
            )

    def test_valid_traits(self) -> None:
        op = WriteOperationConfig(
            path="SENT/_new.yaml",
            operation="send",
            schema_ref="foo.Schema",
            command="+send",
            traits={"reversibility": "none", "confirm": "user"},
        )
        assert op.traits["reversibility"] == "none"
        assert op.traits["confirm"] == "user"


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    def test_duplicate_operation_names(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate operation name"):
            CLIConnectorConfig(
                cli="gws",
                service="gmail",
                auth=AuthConfig(provider="google"),
                write=[
                    WriteOperationConfig(
                        path="SENT/_new.yaml",
                        operation="send_email",
                        schema_ref="foo.Send",
                        command="+send",
                    ),
                    WriteOperationConfig(
                        path="SENT/_reply.yaml",
                        operation="send_email",  # DUPLICATE
                        schema_ref="foo.Reply",
                        command="+reply",
                    ),
                ],
            )

    def test_duplicate_write_paths(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate write path"):
            CLIConnectorConfig(
                cli="gws",
                service="gmail",
                auth=AuthConfig(provider="google"),
                write=[
                    WriteOperationConfig(
                        path="SENT/_new.yaml",  # DUPLICATE
                        operation="send_email",
                        schema_ref="foo.Send",
                        command="+send",
                    ),
                    WriteOperationConfig(
                        path="SENT/_new.yaml",  # DUPLICATE
                        operation="reply_email",
                        schema_ref="foo.Reply",
                        command="+reply",
                    ),
                ],
            )

    def test_unique_operations_pass(self) -> None:
        config = CLIConnectorConfig(
            cli="gws",
            service="gmail",
            auth=AuthConfig(provider="google"),
            write=[
                WriteOperationConfig(
                    path="SENT/_new.yaml",
                    operation="send_email",
                    schema_ref="foo.Send",
                    command="+send",
                ),
                WriteOperationConfig(
                    path="SENT/_reply.yaml",
                    operation="reply_email",
                    schema_ref="foo.Reply",
                    command="+reply",
                ),
            ],
        )
        assert len(config.write) == 2


# ---------------------------------------------------------------------------
# Sync config validation
# ---------------------------------------------------------------------------


class TestSyncConfigValidation:
    def test_page_size_bounds(self) -> None:
        # Too small
        with pytest.raises(ValidationError):
            SyncConfig(
                delta_command="list --after {since}",
                page_size=0,
            )
        # Too large
        with pytest.raises(ValidationError):
            SyncConfig(
                delta_command="list --after {since}",
                page_size=5000,
            )

    def test_valid_sync_config(self) -> None:
        config = SyncConfig(
            delta_command="messages.list --after {since}",
            watch_command="+watch",
            state_field="historyId",
            page_size=200,
        )
        assert config.page_size == 200
        assert config.watch_command == "+watch"


# ---------------------------------------------------------------------------
# Version validation
# ---------------------------------------------------------------------------


class TestVersionValidation:
    def test_version_1_valid(self) -> None:
        config = CLIConnectorConfig(
            version=1,
            cli="gws",
            service="gmail",
            auth=AuthConfig(provider="google"),
        )
        assert config.version == 1

    def test_version_0_invalid(self) -> None:
        with pytest.raises(ValidationError):
            CLIConnectorConfig(
                version=0,
                cli="gws",
                service="gmail",
                auth=AuthConfig(provider="google"),
            )

    def test_version_2_invalid(self) -> None:
        """Version 2 not yet supported."""
        with pytest.raises(ValidationError):
            CLIConnectorConfig(
                version=2,
                cli="gws",
                service="gmail",
                auth=AuthConfig(provider="google"),
            )


# ---------------------------------------------------------------------------
# Read config validation
# ---------------------------------------------------------------------------


class TestReadConfigValidation:
    def test_valid_read_config(self) -> None:
        config = ReadConfig(
            list_command="messages.list",
            get_command="messages.get",
            format="json",
        )
        assert config.format == "json"

    def test_invalid_format(self) -> None:
        with pytest.raises(ValidationError):
            ReadConfig.model_validate(
                {"list_command": "messages.list", "get_command": "messages.get", "format": "xml"}
            )
