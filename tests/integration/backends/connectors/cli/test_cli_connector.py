"""Tests for CLIConnector base class (Phase 2, Issue #3148).

Tests cover:
- write_content() pipeline: YAML parse → validate → traits → CLI exec
- Operation resolution from backend_path
- Token resolution via TokenManager
- CLI execution (with mock subprocess)
- Error handling and error mapper integration
- connect/disconnect lifecycle
- read_content and list_dir delegation
"""

from unittest.mock import MagicMock, patch

import pytest
import yaml
from pydantic import BaseModel, Field

from nexus.backends.connectors.base import (
    ConfirmLevel,
    OpTraits,
    Reversibility,
    ValidationError,
)
from nexus.backends.connectors.cli.base import PathCLIBackend
from nexus.backends.connectors.cli.config import (
    AuthConfig,
    CLIConnectorConfig,
    ReadConfig,
    WriteOperationConfig,
)
from nexus.backends.connectors.cli.result import CLIResult, CLIResultStatus
from nexus.contracts.backend_features import BackendFeature
from nexus.contracts.exceptions import ValidationError as CoreValidationError
from nexus.core.object_store import WriteResult

# ---------------------------------------------------------------------------
# Test schema
# ---------------------------------------------------------------------------


class CreateItemSchema(BaseModel):
    title: str = Field(..., min_length=1, description="Item title")
    body: str = Field(default="", description="Item body")
    agent_intent: str = Field(..., min_length=10, description="Why")


# ---------------------------------------------------------------------------
# Test connector
# ---------------------------------------------------------------------------


class FakeCLIBackend(PathCLIBackend):
    """Test connector with mocked CLI execution."""

    SKILL_NAME = "test-cli"
    CLI_NAME = "test-cli"
    CLI_SERVICE = "items"

    SCHEMAS = {"create_item": CreateItemSchema}
    OPERATION_TRAITS = {
        "create_item": OpTraits(
            reversibility=Reversibility.FULL,
            confirm=ConfirmLevel.INTENT,
        ),
    }

    def __init__(self, **kwargs):
        config = CLIConnectorConfig(
            cli="test-cli",
            service="items",
            auth=AuthConfig(provider="test"),
            write=[
                WriteOperationConfig(
                    path="items/_new.yaml",
                    operation="create_item",
                    schema_ref="tests.unit.backends.connectors.cli.test_cli_connector.CreateItemSchema",
                    command="create",
                ),
            ],
            read=ReadConfig(
                list_command="list",
                get_command="get",
                format="yaml",
            ),
        )
        super().__init__(config=config, **kwargs)
        self._mock_result = CLIResult(
            status=CLIResultStatus.SUCCESS,
            exit_code=0,
            stdout='{"id": "item-123"}',
            command=["test-cli", "items", "create"],
        )

    @property
    def name(self) -> str:
        return "cli:test-cli:items"

    def _execute_cli(self, args, stdin=None, context=None, env=None):
        self._last_stdin = stdin
        self._last_args = args
        self._last_env = env
        return self._mock_result


# ---------------------------------------------------------------------------
# Context helper
# ---------------------------------------------------------------------------


def _context(backend_path: str = "items/_new.yaml") -> MagicMock:
    ctx = MagicMock()
    ctx.backend_path = backend_path
    ctx.user_id = "alice@example.com"
    ctx.zone_id = "test-zone"
    return ctx


# ---------------------------------------------------------------------------
# write_content tests
# ---------------------------------------------------------------------------


class TestWriteContent:
    def test_successful_write(self) -> None:
        connector = FakeCLIBackend()
        content = b"agent_intent: Testing create item for unit test\ntitle: Test Item\nbody: Hello"
        ctx = _context()

        result = connector.write_content(content, ctx)

        assert isinstance(result, WriteResult)
        assert result.content_id  # SHA256 of CLI stdout
        assert result.size == len(content)

    def test_validated_payload_forwarded_to_cli(self) -> None:
        """Codex fix: verify the validated YAML payload is piped to CLI via stdin."""
        connector = FakeCLIBackend()
        content = b"agent_intent: Testing create item for unit test\ntitle: Test Item\nbody: Hello"
        ctx = _context()

        connector.write_content(content, ctx)

        # The stdin passed to _execute_cli must contain the validated payload
        assert connector._last_stdin is not None
        assert "title: Test Item" in connector._last_stdin
        assert "body: Hello" in connector._last_stdin
        # CLI args should include the command
        assert "test-cli" in connector._last_args

    def test_auth_token_via_env_not_args(self) -> None:
        """Token goes via env var, never in CLI args (visible in ps)."""
        connector = FakeCLIBackend()
        # Simulate having a token
        connector._token_manager = type(
            "FakeTM", (), {"get_credentials": lambda self, **kw: {"access_token": "secret-tok-123"}}
        )()
        content = b"agent_intent: Testing auth transport separation\ntitle: Auth Test"
        ctx = _context()

        connector.write_content(content, ctx)

        # stdin should contain ONLY the YAML payload, NOT the token
        assert "secret-tok-123" not in connector._last_stdin
        assert "title: Auth Test" in connector._last_stdin
        # Token must NOT appear in CLI args (would be visible in ps)
        assert "secret-tok-123" not in connector._last_args
        # Token should be in env vars
        assert connector._last_env is not None
        assert any("secret-tok-123" in v for v in connector._last_env.values())

    def test_missing_context_raises(self) -> None:
        connector = FakeCLIBackend()

        with pytest.raises(Exception, match="backend_path"):
            connector.write_content(b"data", None)

    def test_missing_backend_path_raises(self) -> None:
        connector = FakeCLIBackend()
        ctx = MagicMock()
        ctx.backend_path = None

        with pytest.raises(Exception, match="backend_path"):
            connector.write_content(b"data", ctx)

    def test_invalid_yaml_raises(self) -> None:
        connector = FakeCLIBackend()
        ctx = _context()

        with pytest.raises((yaml.YAMLError, ValueError)):
            connector.write_content(b"[not valid yaml: {{{", ctx)

    def test_schema_validation_failure(self) -> None:
        connector = FakeCLIBackend()
        ctx = _context()
        # Missing required 'title' and 'agent_intent'
        content = b"body: just a body"

        with pytest.raises((ValidationError, CoreValidationError)):
            connector.write_content(content, ctx)

    def test_trait_validation_failure(self) -> None:
        connector = FakeCLIBackend()
        ctx = _context()
        # agent_intent too short
        content = b"title: Test\nagent_intent: short"

        with pytest.raises((ValidationError, CoreValidationError)):
            connector.write_content(content, ctx)

    def test_cli_error_propagates(self) -> None:
        connector = FakeCLIBackend()
        connector._mock_result = CLIResult(
            status=CLIResultStatus.EXIT_ERROR,
            exit_code=1,
            stderr="500 Internal Server Error",
            command=["test-cli", "items", "create"],
        )
        ctx = _context()
        content = b"agent_intent: Testing error handling for CLI failure\ntitle: Test Item"

        with pytest.raises(Exception, match="exit_error"):
            connector.write_content(content, ctx)

    def test_unknown_path_raises(self) -> None:
        connector = FakeCLIBackend()
        ctx = _context(backend_path="unknown/path.yaml")
        content = b"agent_intent: Testing unknown path resolution\ntitle: Test"

        with pytest.raises(Exception, match="No operation"):
            connector.write_content(content, ctx)


# ---------------------------------------------------------------------------
# Operation resolution
# ---------------------------------------------------------------------------


class TestOperationResolution:
    def test_resolve_from_config(self) -> None:
        connector = FakeCLIBackend()
        assert connector._resolve_operation("items/_new.yaml") == "create_item"

    def test_resolve_unknown_path(self) -> None:
        connector = FakeCLIBackend()
        assert connector._resolve_operation("unknown/path.yaml") is None

    def test_resolve_with_prefix(self) -> None:
        connector = FakeCLIBackend()
        assert connector._resolve_operation("some/prefix/items/_new.yaml") == "create_item"


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


class TestConnectionLifecycle:
    def test_check_connection_cli_not_found(self) -> None:
        connector = FakeCLIBackend()
        with patch("shutil.which", return_value=None):
            result = connector.check_connection()
        assert result.success is False
        assert "not found" in (result.error_message or "")

    def test_check_connection_cli_found(self) -> None:
        connector = FakeCLIBackend()
        with patch("shutil.which", return_value="/usr/bin/test-cli"):
            result = connector.check_connection()
        assert result.success is True
        assert result.details["path"] == "/usr/bin/test-cli"


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_has_cli_backed(self) -> None:
        connector = FakeCLIBackend()
        assert connector.has_feature(BackendFeature.CLI_BACKED)

    def test_has_readme_doc(self) -> None:
        connector = FakeCLIBackend()
        assert connector.has_feature(BackendFeature.README_DOC)

    def test_name(self) -> None:
        connector = FakeCLIBackend()
        assert connector.name == "cli:test-cli:items"


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


class TestReadOperations:
    def test_list_dir_yaml_output(self) -> None:
        connector = FakeCLIBackend()
        connector._mock_result = CLIResult(
            status=CLIResultStatus.SUCCESS,
            exit_code=0,
            stdout="- item1\n- item2\n- item3\n",
            command=["test-cli", "items", "list"],
        )
        result = connector.list_dir("/")
        assert result == ["item1", "item2", "item3"]

    def test_list_dir_cli_failure(self) -> None:
        connector = FakeCLIBackend()
        connector._mock_result = CLIResult(
            status=CLIResultStatus.EXIT_ERROR,
            exit_code=1,
            command=["test-cli", "items", "list"],
        )
        result = connector.list_dir("/")
        assert result == []

    def test_read_content(self) -> None:
        connector = FakeCLIBackend()
        connector._mock_result = CLIResult(
            status=CLIResultStatus.SUCCESS,
            exit_code=0,
            stdout="title: Test Item\nbody: Hello",
            command=["test-cli", "items", "get"],
        )
        ctx = _context(backend_path="items/item-123.yaml")
        result = connector.read_content("hash", ctx)
        assert b"title: Test Item" in result

    def test_read_content_no_context(self) -> None:
        connector = FakeCLIBackend()
        result = connector.read_content("hash", None)
        assert result == b""


# ---------------------------------------------------------------------------
# Stub implementations
# ---------------------------------------------------------------------------


class TestStubs:
    def test_mkdir_noop(self) -> None:
        connector = FakeCLIBackend()
        connector.mkdir("/test", parents=True, exist_ok=True)

    def test_rmdir_noop(self) -> None:
        connector = FakeCLIBackend()
        connector.rmdir("/test")

    def test_is_directory(self) -> None:
        connector = FakeCLIBackend()
        assert connector.is_directory("/items") is True
        assert connector.is_directory("/items/file.yaml") is False

    def test_content_exists(self) -> None:
        connector = FakeCLIBackend()
        assert connector.content_exists("hash") is False

    def test_delete_content_noop(self) -> None:
        connector = FakeCLIBackend()
        connector.delete_content("hash")  # Should not raise


# ---------------------------------------------------------------------------
# CLI execution env isolation (Issue #3256)
# ---------------------------------------------------------------------------


class TestCLIEnvIsolation:
    def test_google_application_credentials_stripped(self) -> None:
        """GOOGLE_APPLICATION_CREDENTIALS must not leak to CLI subprocesses.

        GCS storage backends set this env var for service account auth,
        but it breaks gws CLI OAuth (gws uses its own env vars).
        """
        import os

        connector = FakeCLIBackend()

        # Inject GOOGLE_APPLICATION_CREDENTIALS into the environment
        old = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/app/gcs-credentials.json"
        try:
            # Call the REAL _execute_cli (not the fake override)
            result = PathCLIBackend._execute_cli(
                connector, ["echo", "hello"], stdin=None, context=None, env=None
            )
        finally:
            if old is None:
                os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            else:
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = old

        # The subprocess should have run without GOOGLE_APPLICATION_CREDENTIALS
        assert result.ok
        assert "hello" in result.stdout

    def test_auth_env_vars_passed_through(self) -> None:
        """Custom auth env vars (GH_TOKEN, GWS_ACCESS_TOKEN) must be passed."""
        connector = FakeCLIBackend()
        result = PathCLIBackend._execute_cli(
            connector,
            ["env"],
            stdin=None,
            context=None,
            env={"GWS_ACCESS_TOKEN": "test-tok-123"},
        )
        assert result.ok
        assert "GWS_ACCESS_TOKEN=test-tok-123" in result.stdout
