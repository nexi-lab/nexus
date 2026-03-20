"""Contract tests for DelegatingBackend transform hooks (#2077).

Tests verify:
1. Default _transform_on_read is identity (pass-through)
2. Default _transform_on_write is identity (pass-through)
3. read_content applies _transform_on_read after inner.read_content
4. write_content applies _transform_on_write before inner.write_content
5. write_content catches exceptions from _transform_on_write
6. batch_read_content applies _transform_on_read to each item
7. batch_read_content handles transform failures per-item
8. __getattr__ correctly delegates to inner (#2077, Issue 8)

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16, Recursive Wrapping (Mechanism 2)
    - Issue #2077: Deduplicate backend wrapper boilerplate
"""

from unittest.mock import MagicMock

import pytest

from nexus.backends.base.backend import HandlerStatusResponse
from nexus.backends.storage.delegating import DelegatingBackend
from nexus.contracts.exceptions import NexusFileNotFoundError
from nexus.core.object_store import WriteResult
from tests.unit.backends.wrapper_test_helpers import make_leaf, make_storage_mock

# ---------------------------------------------------------------------------
# FakeTransformBackend — test double for hook contract tests
# ---------------------------------------------------------------------------


class FakeTransformBackend(DelegatingBackend):
    """Test wrapper that applies a simple reversible transform.

    Write: prepend b"TX:" prefix.
    Read: strip b"TX:" prefix.
    """

    def describe(self) -> str:
        return f"fake-transform → {self._inner.describe()}"

    @property
    def name(self) -> str:
        return f"fake-transform({self._inner.name})"

    def _transform_on_write(self, content: bytes) -> bytes:
        return b"TX:" + content

    def _transform_on_read(self, data: bytes) -> bytes:
        if not data.startswith(b"TX:"):
            raise ValueError("Missing TX: prefix")
        return data[3:]


class FailingWriteBackend(DelegatingBackend):
    """Test wrapper whose _transform_on_write always raises."""

    def describe(self) -> str:
        return f"failing-write → {self._inner.describe()}"

    @property
    def name(self) -> str:
        return f"failing-write({self._inner.name})"

    def _transform_on_write(self, content: bytes) -> bytes:
        raise RuntimeError("write transform broken")


class FailingReadBackend(DelegatingBackend):
    """Test wrapper whose _transform_on_read always raises."""

    def describe(self) -> str:
        return f"failing-read → {self._inner.describe()}"

    @property
    def name(self) -> str:
        return f"failing-read({self._inner.name})"

    def _transform_on_read(self, data: bytes) -> bytes:
        raise RuntimeError("read transform broken")


# ---------------------------------------------------------------------------
# Default Hooks (Identity)
# ---------------------------------------------------------------------------


class TestDefaultHooks:
    """DelegatingBackend's default hooks should be identity transforms."""

    def test_default_transform_on_write_is_identity(self) -> None:
        leaf = make_leaf()
        wrapper = DelegatingBackend(leaf)
        assert wrapper._transform_on_write(b"hello") == b"hello"

    def test_default_transform_on_read_is_identity(self) -> None:
        leaf = make_leaf()
        wrapper = DelegatingBackend(leaf)
        assert wrapper._transform_on_read(b"hello") == b"hello"

    def test_passthrough_write_delegates_to_inner(self) -> None:
        leaf = make_leaf()
        expected = WriteResult(content_id="hash123")
        leaf.write_content.return_value = expected
        wrapper = DelegatingBackend(leaf)

        result = wrapper.write_content(b"hello")
        leaf.write_content.assert_called_once_with(b"hello", "", context=None)
        assert result is expected

    def test_passthrough_read_delegates_to_inner(self) -> None:
        leaf = make_leaf()
        expected = b"content"
        leaf.read_content.return_value = expected
        wrapper = DelegatingBackend(leaf)

        result = wrapper.read_content("hash123")
        leaf.read_content.assert_called_once_with("hash123", context=None)
        assert result == b"content"


# ---------------------------------------------------------------------------
# Transform Hook Contract Tests
# ---------------------------------------------------------------------------


class TestTransformOnWrite:
    """write_content should apply _transform_on_write before delegating."""

    def test_transforms_content_before_inner_write(self) -> None:
        mock, storage = make_storage_mock()
        wrapper = FakeTransformBackend(mock)

        resp = wrapper.write_content(b"hello")
        assert isinstance(resp, WriteResult)

        # Inner should receive transformed content
        call_args = mock.write_content.call_args
        written_content = call_args[0][0]
        assert written_content == b"TX:hello"

    def test_error_on_transform_failure(self) -> None:
        leaf = make_leaf()
        wrapper = FailingWriteBackend(leaf)

        with pytest.raises(RuntimeError, match="write transform broken"):
            wrapper.write_content(b"hello")
        # Inner should NOT have been called
        leaf.write_content.assert_not_called()


class TestTransformOnRead:
    """read_content should apply _transform_on_read after inner read."""

    def test_transforms_content_after_inner_read(self) -> None:
        mock, storage = make_storage_mock()
        wrapper = FakeTransformBackend(mock)

        # Write through wrapper (stores "TX:hello")
        write_resp = wrapper.write_content(b"hello")
        assert isinstance(write_resp, WriteResult)

        # Read through wrapper (strips "TX:" prefix)
        read_resp = wrapper.read_content(write_resp.content_id)
        assert read_resp == b"hello"

    def test_error_response_propagated_from_inner(self) -> None:
        leaf = make_leaf()
        leaf.read_content.side_effect = NexusFileNotFoundError("nonexistent")
        wrapper = FakeTransformBackend(leaf)

        with pytest.raises(NexusFileNotFoundError):
            wrapper.read_content("nonexistent")

    def test_error_on_transform_failure(self) -> None:
        leaf = make_leaf()
        leaf.read_content.return_value = b"no-prefix"
        wrapper = FakeTransformBackend(leaf)

        with pytest.raises(ValueError, match="Missing TX: prefix"):
            wrapper.read_content("some-hash")


# ---------------------------------------------------------------------------
# Batch Read with Transform
# ---------------------------------------------------------------------------


class TestBatchReadWithTransform:
    """batch_read_content should apply _transform_on_read per item."""

    def test_transforms_each_item(self) -> None:
        mock, storage = make_storage_mock()
        wrapper = FakeTransformBackend(mock)

        h1 = wrapper.write_content(b"alpha").content_id
        h2 = wrapper.write_content(b"beta").content_id

        results = wrapper.batch_read_content([h1, h2])
        assert results[h1] == b"alpha"
        assert results[h2] == b"beta"

    def test_handles_missing_items(self) -> None:
        mock, storage = make_storage_mock()
        wrapper = FakeTransformBackend(mock)

        h1 = wrapper.write_content(b"exists").content_id
        results = wrapper.batch_read_content([h1, "missing"])
        assert results[h1] == b"exists"
        assert results["missing"] is None

    def test_per_item_transform_failure_returns_none(self) -> None:
        """If transform fails for one item, that item becomes None."""
        leaf = make_leaf()
        # Return data without TX: prefix for one item
        leaf.batch_read_content.return_value = {
            "good": b"TX:valid",
            "bad": b"no-prefix",
        }
        wrapper = FakeTransformBackend(leaf)

        results = wrapper.batch_read_content(["good", "bad"])
        assert results["good"] == b"valid"
        assert results["bad"] is None  # Transform failure → None


# ---------------------------------------------------------------------------
# __getattr__ Delegation Tests (#2077, Issue 8)
# ---------------------------------------------------------------------------


class TestGetAttrDelegation:
    """__getattr__ should delegate non-overridden attributes to inner."""

    def test_delegates_custom_attribute(self) -> None:
        leaf = make_leaf()
        leaf.custom_method = MagicMock(return_value="custom-result")
        wrapper = DelegatingBackend(leaf)

        result = wrapper.custom_method("arg1")
        assert result == "custom-result"
        leaf.custom_method.assert_called_once_with("arg1")

    def test_delegates_another_custom_attribute(self) -> None:
        leaf = make_leaf()
        leaf.my_plugin_method = MagicMock(return_value="plugin-result")
        wrapper = DelegatingBackend(leaf)

        result = wrapper.my_plugin_method("arg")
        assert result == "plugin-result"
        leaf.my_plugin_method.assert_called_once_with("arg")

    def test_missing_attribute_raises_attribute_error(self) -> None:
        leaf = make_leaf()
        wrapper = DelegatingBackend(leaf)

        with pytest.raises(AttributeError):
            wrapper.nonexistent_method()  # type: ignore[attr-defined]

    def test_check_connection_delegates(self) -> None:
        leaf = make_leaf()
        expected = HandlerStatusResponse(success=True)
        leaf.check_connection.return_value = expected
        wrapper = DelegatingBackend(leaf)

        result = wrapper.check_connection()
        assert result is expected
