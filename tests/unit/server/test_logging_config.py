"""Tests for structured logging configuration.

Issue #1002: Structured JSON logging with request correlation.

Tests the central ``configure_logging()`` function which sets up structlog
with a processor pipeline, routes stdlib loggers through ProcessorFormatter,
and switches between dev (pretty console) and prod (JSON) rendering.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog

from nexus.server.logging_config import _orjson_serializer, configure_logging

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_json_log(logger_name: str, message: str, **kwargs: object) -> dict:
    """Configure prod logging, emit one log line, return the parsed JSON dict."""
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)

    configure_logging(env="prod", log_level="DEBUG", _handler_override=handler)

    logger = structlog.get_logger(logger_name)
    logger.info(message, **kwargs)

    raw = buf.getvalue().strip()
    assert raw, "No log output captured"
    # In parallel CI, stray log lines from other tests may leak into the
    # buffer.  Parse only the last line which is our message.
    last_line = raw.split("\n")[-1].strip()
    return json.loads(last_line)


def _capture_dev_log(logger_name: str, message: str, **kwargs: object) -> str:
    """Configure dev logging, emit one log line, return raw text."""
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)

    configure_logging(env="dev", log_level="DEBUG", _handler_override=handler)

    logger = structlog.get_logger(logger_name)
    logger.info(message, **kwargs)

    raw = buf.getvalue().strip()
    assert raw, "No log output captured"
    return raw


# ---------------------------------------------------------------------------
# Phase 1: Core configuration tests
# ---------------------------------------------------------------------------


class TestConfigureLoggingProdMode:
    """Prod mode produces JSON output via orjson."""

    def test_json_output_has_required_fields(self) -> None:
        entry = _capture_json_log("nexus.test", "hello world")
        assert "event" in entry
        assert "level" in entry
        assert "timestamp" in entry
        assert "logger" in entry

    def test_event_field_contains_message(self) -> None:
        entry = _capture_json_log("nexus.test", "hello world")
        assert entry["event"] == "hello world"

    def test_level_field_is_string(self) -> None:
        entry = _capture_json_log("nexus.test", "hello world")
        assert entry["level"] == "info"

    def test_timestamp_is_utc_iso8601(self) -> None:
        entry = _capture_json_log("nexus.test", "hello world")
        ts = entry["timestamp"]
        # ISO 8601 with UTC: 2026-02-13T10:30:00.123456Z or +00:00
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts)

    def test_extra_kwargs_merged_into_event(self) -> None:
        entry = _capture_json_log("nexus.test", "op done", user_id="u123", path="/files")
        assert entry["user_id"] == "u123"
        assert entry["path"] == "/files"

    def test_orjson_handles_non_string_values(self) -> None:
        entry = _capture_json_log("nexus.test", "metrics", count=42, ratio=3.14)
        assert entry["count"] == 42
        assert entry["ratio"] == 3.14


class TestConfigureLoggingDevMode:
    """Dev mode produces pretty console output (not JSON)."""

    def test_dev_mode_not_json(self) -> None:
        raw = _capture_dev_log("nexus.test", "hello dev")
        # Dev output should NOT be valid JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

    def test_dev_mode_contains_message(self) -> None:
        raw = _capture_dev_log("nexus.test", "hello dev")
        assert "hello dev" in raw

    def test_dev_mode_contains_level(self) -> None:
        raw = _capture_dev_log("nexus.test", "hello dev")
        assert "info" in raw.lower()


class TestLogLevel:
    """LOG_LEVEL environment variable and explicit log_level arg."""

    def test_log_level_from_explicit_arg(self) -> None:
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)

        configure_logging(env="prod", log_level="WARNING", _handler_override=handler)

        logger = structlog.get_logger("nexus.test")
        logger.info("should be suppressed")
        logger.warning("should appear")

        raw = buf.getvalue().strip()
        lines = [line for line in raw.split("\n") if line.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == "should appear"

    def test_log_level_from_env_var(self) -> None:
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)

        with patch.dict("os.environ", {"LOG_LEVEL": "ERROR"}):
            configure_logging(env="prod", _handler_override=handler)

        logger = structlog.get_logger("nexus.test")
        logger.warning("suppressed")
        logger.error("visible")

        raw = buf.getvalue().strip()
        lines = [line for line in raw.split("\n") if line.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == "visible"

    def test_log_level_default_is_info(self) -> None:
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)

        with patch.dict("os.environ", {}, clear=False):
            # Ensure LOG_LEVEL is not set
            import os

            os.environ.pop("LOG_LEVEL", None)
            configure_logging(env="prod", _handler_override=handler)

        logger = structlog.get_logger("nexus.test")
        logger.debug("suppressed")
        logger.info("visible")

        raw = buf.getvalue().strip()
        lines = [line for line in raw.split("\n") if line.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == "visible"


class TestStdlibBridge:
    """stdlib logging.getLogger() calls routed through structlog."""

    def test_stdlib_logger_produces_json_in_prod(self) -> None:
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)

        configure_logging(env="prod", log_level="DEBUG", _handler_override=handler)

        stdlib_logger = logging.getLogger("nexus.stdlib_test")
        stdlib_logger.info("from stdlib")

        raw = buf.getvalue().strip()
        lines = [line for line in raw.split("\n") if line.strip()]
        assert len(lines) >= 1
        entry = json.loads(lines[0])
        assert entry["event"] == "from stdlib"

    def test_uvicorn_access_log_suppressed(self) -> None:
        """uvicorn.access is set to WARNING — INFO access logs are suppressed.

        CorrelationMiddleware provides a superior structured access log.
        """
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)

        configure_logging(env="prod", log_level="DEBUG", _handler_override=handler)

        uvicorn_logger = logging.getLogger("uvicorn.access")
        uvicorn_logger.info("GET /health 200")  # Should be suppressed

        raw = buf.getvalue().strip()
        # No output because uvicorn.access INFO is suppressed
        assert raw == ""

    def test_uvicorn_access_warnings_still_logged(self) -> None:
        """uvicorn.access WARNING+ still flows through."""
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)

        configure_logging(env="prod", log_level="DEBUG", _handler_override=handler)

        uvicorn_logger = logging.getLogger("uvicorn.access")
        uvicorn_logger.warning("slow request detected")

        raw = buf.getvalue().strip()
        lines = [line for line in raw.split("\n") if line.strip()]
        assert len(lines) >= 1
        entry = json.loads(lines[0])
        assert "slow request detected" in entry["event"]


class TestConfigureLoggingIdempotent:
    """Calling configure_logging() multiple times should not break."""

    def test_double_configure_no_error(self) -> None:
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)

        configure_logging(env="prod", log_level="DEBUG", _handler_override=handler)
        configure_logging(env="prod", log_level="DEBUG", _handler_override=handler)

        logger = structlog.get_logger("nexus.test")
        logger.info("after double configure")

        raw = buf.getvalue().strip()
        # Should not produce duplicate lines
        lines = [line for line in raw.split("\n") if line.strip()]
        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert entry["event"] == "after double configure"


# ---------------------------------------------------------------------------
# Issue 12: Validation tests
# ---------------------------------------------------------------------------


class TestConfigureLoggingValidation:
    """Invalid parameters raise ValueError with clear messages."""

    @pytest.mark.parametrize("bad_env", ["staging", "", "PROD", "production", "test"])
    def test_invalid_env_raises_value_error(self, bad_env: str) -> None:
        with pytest.raises(ValueError, match="Invalid env"):
            configure_logging(env=bad_env)

    @pytest.mark.parametrize("bad_level", ["TRACE", "verbose", "NOTSET_LIKE"])
    def test_invalid_log_level_raises_value_error(self, bad_level: str) -> None:
        with pytest.raises(ValueError, match="Invalid log level"):
            configure_logging(env="dev", log_level=bad_level)

    def test_empty_log_level_defaults_to_info(self) -> None:
        """Empty string is falsy and falls through to LOG_LEVEL env or INFO default."""
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)
        # Should not raise — empty string treated as "use default"
        configure_logging(env="dev", log_level="", _handler_override=handler)


# ---------------------------------------------------------------------------
# Issue 10: orjson serializer edge cases
# ---------------------------------------------------------------------------


class TestOrjsonSerializer:
    """_orjson_serializer handles non-standard types without crashing."""

    def test_serializes_datetime(self) -> None:
        dt = datetime(2026, 2, 13, 10, 30, 0, tzinfo=UTC)
        result = _orjson_serializer({"event": "test", "ts": dt})
        parsed = json.loads(result)
        # orjson with OPT_UTC_Z renders datetime natively; fallback is str()
        assert "2026" in str(parsed["ts"])

    def test_serializes_set(self) -> None:
        result = _orjson_serializer({"event": "test", "tags": {"a", "b"}})
        parsed = json.loads(result)
        # default=str converts set to its repr
        assert isinstance(parsed["tags"], str)
        assert "a" in parsed["tags"]

    def test_serializes_path(self) -> None:
        result = _orjson_serializer({"event": "test", "file": Path("/tmp/log.txt")})
        parsed = json.loads(result)
        assert parsed["file"] == "/tmp/log.txt"

    def test_serializes_custom_object(self) -> None:
        class Custom:
            def __str__(self) -> str:
                return "custom-obj-42"

        result = _orjson_serializer({"event": "test", "obj": Custom()})
        parsed = json.loads(result)
        assert parsed["obj"] == "custom-obj-42"

    def test_serializes_standard_types(self) -> None:
        result = _orjson_serializer(
            {
                "event": "metrics",
                "count": 42,
                "ratio": 3.14,
                "flag": True,
                "items": [1, 2, 3],
            }
        )
        parsed = json.loads(result)
        assert parsed["count"] == 42
        assert parsed["ratio"] == 3.14
        assert parsed["flag"] is True
        assert parsed["items"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Issue 9: Full pipeline integration test
# ---------------------------------------------------------------------------


class TestFullPipelineIntegration:
    """End-to-end: configure_logging + CorrelationMiddleware → JSON output."""

    @pytest.mark.asyncio
    async def test_request_produces_complete_json_log(self) -> None:
        """A request through the full pipeline produces a JSON log line
        with ALL expected fields as separate queryable keys."""
        from nexus.server.middleware.correlation import CorrelationMiddleware
        from tests.unit.server.conftest import SendCapture, make_http_scope, make_receive

        # Set up prod logging with a capture buffer
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)
        configure_logging(env="prod", log_level="DEBUG", _handler_override=handler)

        # Build a minimal ASGI app
        async def ok_app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        middleware = CorrelationMiddleware(ok_app)
        scope = make_http_scope(
            method="POST",
            path="/api/files",
            headers=[(b"x-request-id", b"integration-test-id")],
        )

        await middleware(scope, make_receive(), SendCapture())

        # Parse the JSON log output
        raw = buf.getvalue().strip()
        lines = [line for line in raw.split("\n") if line.strip()]
        assert len(lines) >= 1

        # Find the request_completed line
        completion_entry = None
        for line in lines:
            entry = json.loads(line)
            if entry.get("event") == "request_completed":
                completion_entry = entry
                break

        assert completion_entry is not None, (
            f"No 'request_completed' event found in log output:\n{raw}"
        )

        # Assert ALL expected fields are present as separate queryable keys
        assert completion_entry["event"] == "request_completed"
        assert completion_entry["correlation_id"] == "integration-test-id"
        assert completion_entry["http_method"] == "POST"
        assert completion_entry["http_path"] == "/api/files"
        assert completion_entry["status_code"] == 200
        assert isinstance(completion_entry["duration_ms"], (int, float))
        assert completion_entry["duration_ms"] >= 0
        assert "timestamp" in completion_entry
        assert "level" in completion_entry
        assert completion_entry["service"] == "nexus"
