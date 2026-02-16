"""Tests for the @timed_response decorator.

Validates that the decorator correctly:
- Sets execution_time_ms on success responses
- Catches exceptions and returns HandlerResponse.from_exception() with timing
- Preserves already-timed responses
- Extracts backend name from self.name
- Handles None data in responses
"""

from __future__ import annotations

import time

import pytest

from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.response import HandlerResponse, ResponseType, timed_response

# === Fixtures ===


class FakeBackend:
    """Minimal backend-like object for testing @timed_response."""

    name = "fake"

    @timed_response
    def succeed(self, value: str) -> HandlerResponse[str]:
        return HandlerResponse.ok(data=value, backend_name=self.name)

    @timed_response
    def succeed_with_sleep(self, ms: float) -> HandlerResponse[str]:
        time.sleep(ms / 1000)
        return HandlerResponse.ok(data="done", backend_name=self.name)

    @timed_response
    def fail_with_exception(self) -> HandlerResponse[str]:
        raise ValueError("something went wrong")

    @timed_response
    def fail_with_file_not_found(self) -> HandlerResponse[bytes]:
        raise NexusFileNotFoundError(path="/missing.txt")

    @timed_response
    def return_none_data(self) -> HandlerResponse[None]:
        return HandlerResponse.ok(data=None, backend_name=self.name)

    @timed_response
    def return_already_timed(self) -> HandlerResponse[str]:
        return HandlerResponse.ok(data="pre-timed", execution_time_ms=42.0, backend_name=self.name)

    @timed_response
    def fail_with_backend_error(self) -> HandlerResponse[str]:
        raise BackendError(message="disk full", backend="fake", path="/data")


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


# === Happy Path Tests ===


class TestTimedResponseHappyPath:
    def test_sets_execution_time_on_success(self, backend: FakeBackend) -> None:
        response = backend.succeed("hello")
        assert response.success
        assert response.data == "hello"
        assert response.execution_time_ms > 0

    def test_execution_time_is_reasonable(self, backend: FakeBackend) -> None:
        response = backend.succeed_with_sleep(10)
        assert response.execution_time_ms >= 9  # Allow small timing variance

    def test_backend_name_preserved(self, backend: FakeBackend) -> None:
        response = backend.succeed("test")
        assert response.backend_name == "fake"


# === Error Path Tests ===


class TestTimedResponseErrorPath:
    def test_exception_caught_returns_error_response(self, backend: FakeBackend) -> None:
        response = backend.fail_with_exception()
        assert not response.success
        assert response.resp_type == ResponseType.ERROR
        assert response.error_message is not None
        assert "something went wrong" in response.error_message
        assert response.execution_time_ms > 0

    def test_file_not_found_mapped_correctly(self, backend: FakeBackend) -> None:
        response = backend.fail_with_file_not_found()
        assert not response.success
        assert response.resp_type == ResponseType.NOT_FOUND
        assert response.execution_time_ms > 0

    def test_backend_name_extracted_on_error(self, backend: FakeBackend) -> None:
        response = backend.fail_with_exception()
        assert response.backend_name == "fake"

    def test_backend_error_mapped_correctly(self, backend: FakeBackend) -> None:
        response = backend.fail_with_backend_error()
        assert not response.success
        assert response.resp_type == ResponseType.ERROR
        assert response.execution_time_ms > 0


# === Edge Cases ===


class TestTimedResponseEdgeCases:
    def test_none_data_gets_timing(self, backend: FakeBackend) -> None:
        response = backend.return_none_data()
        assert response.success
        assert response.data is None
        assert response.execution_time_ms > 0

    def test_already_timed_not_overwritten(self, backend: FakeBackend) -> None:
        response = backend.return_already_timed()
        assert response.success
        assert response.execution_time_ms == 42.0

    def test_backend_name_extraction_no_args(self) -> None:
        """When called without self (bare function), backend_name is None."""

        @timed_response
        def bare_function() -> HandlerResponse[str]:
            raise ValueError("oops")

        response = bare_function()
        assert not response.success
        # No args[0] to extract name from — should not crash
        assert response.backend_name is None

    def test_backend_name_extraction_no_name_attr(self) -> None:
        """When self has no .name attribute, backend_name is None."""

        class NoName:
            @timed_response
            def do_thing(self) -> HandlerResponse[str]:
                raise ValueError("oops")

        obj = NoName()
        response = obj.do_thing()
        assert not response.success
        assert response.backend_name is None
