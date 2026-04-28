"""Unit tests for BatchExecutor (Issue #1242).

Tests the batch operation executor in isolation with a mock NexusFS.
Covers all 13 edge case scenarios identified in the architecture review.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import (
    InvalidPathError,
    NexusFileNotFoundError,
    NexusPermissionError,
)
from nexus.server.batch_executor import (
    BatchExecutor,
    BatchRequest,
    DeleteOp,
    ExistsOp,
    ListOp,
    MkdirOp,
    ReadOp,
    StatOp,
    WriteOp,
)


@pytest.fixture()
def mock_fs() -> MagicMock:
    """Create a mock NexusFS with default behaviors."""
    fs = MagicMock()
    # Default behaviors for each operation
    fs.read.return_value = b"file content"
    fs.write.return_value = {
        "content_id": "abc123",
        "version": 1,
        "size": 12,
        "modified_at": "2026-02-17T00:00:00Z",
        "path": "/test.txt",
    }
    fs.delete.return_value = {"deleted": True, "path": "/test.txt"}
    fs.exists.return_value = True
    fs.list.return_value = ["/file1.txt", "/file2.txt"]
    fs.mkdir.return_value = None

    # Metadata mock
    meta = MagicMock()
    meta.path = "/test.txt"
    meta.size = 12
    meta.content_id = "abc123"
    meta.version = 1
    meta.is_dir = False
    meta.created_at = None
    meta.modified_at = None
    fs.get_metadata.return_value = meta
    return fs


@pytest.fixture()
def mock_context() -> MagicMock:
    """Create a mock OperationContext."""
    ctx = MagicMock()
    ctx.user_id = "test-user"
    ctx.zone_id = ROOT_ZONE_ID
    ctx.groups = []
    ctx.is_admin = False
    ctx.is_system = False
    return ctx


@pytest.fixture()
def executor(mock_fs: MagicMock) -> BatchExecutor:
    """Create a BatchExecutor with mock FS."""
    return BatchExecutor(fs=mock_fs)


# =============================================================================
# Test 1: Happy path — all 7 operation types succeed
# =============================================================================


class TestHappyPath:
    """Test that all 7 operation types succeed individually."""

    @pytest.mark.asyncio()
    async def test_read_operation(self, executor: BatchExecutor, mock_context: MagicMock) -> None:
        request = BatchRequest(
            operations=[ReadOp(path="/test.txt")],
        )
        response = await executor.execute(request, context=mock_context)
        assert len(response.results) == 1
        assert response.results[0].status == 200
        assert response.results[0].data is not None
        assert "content" in response.results[0].data

    @pytest.mark.asyncio()
    async def test_write_operation(self, executor: BatchExecutor, mock_context: MagicMock) -> None:
        request = BatchRequest(
            operations=[WriteOp(path="/test.txt", content="hello")],
        )
        response = await executor.execute(request, context=mock_context)
        assert len(response.results) == 1
        assert response.results[0].status == 201
        assert response.results[0].data is not None

    @pytest.mark.asyncio()
    async def test_delete_operation(self, executor: BatchExecutor, mock_context: MagicMock) -> None:
        request = BatchRequest(
            operations=[DeleteOp(path="/test.txt")],
        )
        response = await executor.execute(request, context=mock_context)
        assert len(response.results) == 1
        assert response.results[0].status == 200

    @pytest.mark.asyncio()
    async def test_stat_operation(self, executor: BatchExecutor, mock_context: MagicMock) -> None:
        request = BatchRequest(
            operations=[StatOp(path="/test.txt")],
        )
        response = await executor.execute(request, context=mock_context)
        assert len(response.results) == 1
        assert response.results[0].status == 200
        assert response.results[0].data is not None
        assert "size" in response.results[0].data

    @pytest.mark.asyncio()
    async def test_exists_operation(self, executor: BatchExecutor, mock_context: MagicMock) -> None:
        request = BatchRequest(
            operations=[ExistsOp(path="/test.txt")],
        )
        response = await executor.execute(request, context=mock_context)
        assert len(response.results) == 1
        assert response.results[0].status == 200
        assert response.results[0].data == {"exists": True}

    @pytest.mark.asyncio()
    async def test_list_operation(self, executor: BatchExecutor, mock_context: MagicMock) -> None:
        request = BatchRequest(
            operations=[ListOp(path="/")],
        )
        response = await executor.execute(request, context=mock_context)
        assert len(response.results) == 1
        assert response.results[0].status == 200
        assert response.results[0].data is not None
        assert "items" in response.results[0].data

    @pytest.mark.asyncio()
    async def test_mkdir_operation(self, executor: BatchExecutor, mock_context: MagicMock) -> None:
        request = BatchRequest(
            operations=[MkdirOp(path="/newdir")],
        )
        response = await executor.execute(request, context=mock_context)
        assert len(response.results) == 1
        assert response.results[0].status == 201

    @pytest.mark.asyncio()
    async def test_all_ops_in_one_batch(
        self, executor: BatchExecutor, mock_context: MagicMock
    ) -> None:
        """All 7 operation types in a single batch."""
        request = BatchRequest(
            operations=[
                ReadOp(path="/test.txt"),
                WriteOp(path="/new.txt", content="data"),
                DeleteOp(path="/old.txt"),
                StatOp(path="/test.txt"),
                ExistsOp(path="/test.txt"),
                ListOp(path="/"),
                MkdirOp(path="/newdir"),
            ],
        )
        response = await executor.execute(request, context=mock_context)
        assert len(response.results) == 7
        # All should succeed
        for result in response.results:
            assert result.status < 400, f"Op at index {result.index} failed: {result.error}"


# =============================================================================
# Test 2: Partial failure — some succeed, some fail
# =============================================================================


class TestPartialFailure:
    @pytest.mark.asyncio()
    async def test_read_succeeds_write_fails_stat_succeeds(
        self, mock_fs: MagicMock, mock_context: MagicMock
    ) -> None:
        """Read succeeds, write fails (permission), stat succeeds."""
        mock_fs.write.side_effect = NexusPermissionError("No write permission")
        executor = BatchExecutor(fs=mock_fs)

        request = BatchRequest(
            operations=[
                ReadOp(path="/test.txt"),
                WriteOp(path="/protected.txt", content="data"),
                StatOp(path="/test.txt"),
            ],
        )
        response = await executor.execute(request, context=mock_context)
        assert len(response.results) == 3
        assert response.results[0].status == 200  # read succeeded
        assert response.results[1].status == 403  # write permission denied
        assert response.results[2].status == 200  # stat succeeded (batch continued)


# =============================================================================
# Test 3: stop_on_error — first op fails, remaining skipped
# =============================================================================


class TestStopOnError:
    @pytest.mark.asyncio()
    async def test_first_op_fails_remaining_skipped(
        self, mock_fs: MagicMock, mock_context: MagicMock
    ) -> None:
        mock_fs.read.side_effect = NexusPermissionError("No read permission")
        executor = BatchExecutor(fs=mock_fs)

        request = BatchRequest(
            operations=[
                ReadOp(path="/protected.txt"),
                WriteOp(path="/other.txt", content="data"),
                StatOp(path="/test.txt"),
            ],
            stop_on_error=True,
        )
        response = await executor.execute(request, context=mock_context)
        assert len(response.results) == 3
        assert response.results[0].status == 403  # failed
        assert response.results[1].status == 424  # skipped
        assert response.results[2].status == 424  # skipped
        assert response.results[1].error is not None
        assert "skipped" in response.results[1].error.lower()

    @pytest.mark.asyncio()
    async def test_middle_op_fails_earlier_kept_later_skipped(
        self, mock_fs: MagicMock, mock_context: MagicMock
    ) -> None:
        """First op succeeds, second fails, third skipped."""
        call_count = 0

        def failing_on_second_read(path: str, **kwargs: Any) -> bytes:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise NexusFileNotFoundError(path=path)
            return b"content"

        mock_fs.read.side_effect = failing_on_second_read
        executor = BatchExecutor(fs=mock_fs)

        request = BatchRequest(
            operations=[
                ReadOp(path="/exists.txt"),
                ReadOp(path="/missing.txt"),
                ReadOp(path="/other.txt"),
            ],
            stop_on_error=True,
        )
        response = await executor.execute(request, context=mock_context)
        assert len(response.results) == 3
        assert response.results[0].status == 200  # first succeeded
        assert response.results[1].status == 404  # second failed
        assert response.results[2].status == 424  # third skipped


# =============================================================================
# Test 4: Empty batch → validation error
# =============================================================================


class TestValidation:
    def test_empty_operations_rejected(self) -> None:
        """Empty operations list should fail Pydantic validation."""
        with pytest.raises(ValidationError):
            BatchRequest(operations=[])

    def test_exceeds_max_operations(self) -> None:
        """More than 50 operations should fail Pydantic validation."""
        ops = [ReadOp(path=f"/file{i}.txt") for i in range(51)]
        with pytest.raises(ValidationError):
            BatchRequest(operations=ops)

    def test_exactly_50_operations_accepted(self) -> None:
        """Exactly 50 operations should be accepted."""
        ops = [ReadOp(path=f"/file{i}.txt") for i in range(50)]
        request = BatchRequest(operations=ops)
        assert len(request.operations) == 50

    def test_exactly_1_operation_accepted(self) -> None:
        """Single operation should be accepted."""
        request = BatchRequest(operations=[ReadOp(path="/file.txt")])
        assert len(request.operations) == 1


# =============================================================================
# Test 5: All failures — every op fails, batch still returns 200-level structure
# =============================================================================


class TestAllFailures:
    @pytest.mark.asyncio()
    async def test_all_operations_fail(self, mock_fs: MagicMock, mock_context: MagicMock) -> None:
        mock_fs.read.side_effect = NexusFileNotFoundError(path="/missing.txt")
        mock_fs.write.side_effect = NexusPermissionError("denied")
        mock_fs.delete.side_effect = NexusFileNotFoundError(path="/missing.txt")
        executor = BatchExecutor(fs=mock_fs)

        request = BatchRequest(
            operations=[
                ReadOp(path="/missing.txt"),
                WriteOp(path="/protected.txt", content="data"),
                DeleteOp(path="/missing.txt"),
            ],
        )
        response = await executor.execute(request, context=mock_context)
        assert len(response.results) == 3
        # All should have error status codes
        assert response.results[0].status == 404
        assert response.results[1].status == 403
        assert response.results[2].status == 404
        # All should have error messages
        for result in response.results:
            assert result.error is not None


# =============================================================================
# Test 6: Permission per-operation — Op 1 has permission, Op 2 doesn't
# =============================================================================


class TestPerOperationPermissions:
    @pytest.mark.asyncio()
    async def test_mixed_permission_results(
        self, mock_fs: MagicMock, mock_context: MagicMock
    ) -> None:
        """First read succeeds, second read denied by permission."""
        call_count = 0

        def permission_gated_read(path: str, **kwargs: Any) -> bytes:
            nonlocal call_count
            call_count += 1
            if path == "/protected.txt":
                raise NexusPermissionError("Access denied")
            return b"content"

        mock_fs.read.side_effect = permission_gated_read
        executor = BatchExecutor(fs=mock_fs)

        request = BatchRequest(
            operations=[
                ReadOp(path="/public.txt"),
                ReadOp(path="/protected.txt"),
            ],
        )
        response = await executor.execute(request, context=mock_context)
        assert response.results[0].status == 200
        assert response.results[1].status == 403


# =============================================================================
# Test 7: Sequential dependency — write then read same file
# =============================================================================


class TestSequentialDependency:
    @pytest.mark.asyncio()
    async def test_write_then_read_same_file(
        self, mock_fs: MagicMock, mock_context: MagicMock
    ) -> None:
        """Write and read should execute in order."""
        execution_order: list[str] = []

        def tracking_write(path: str, content: Any, **kwargs: Any) -> dict:
            execution_order.append(f"write:{path}")
            return {
                "content_id": "new",
                "version": 1,
                "size": len(str(content)),
                "modified_at": "2026-02-17T00:00:00Z",
                "path": path,
            }

        def tracking_read(path: str, **kwargs: Any) -> bytes:
            execution_order.append(f"read:{path}")
            return b"written content"

        mock_fs.write.side_effect = tracking_write
        mock_fs.read.side_effect = tracking_read
        executor = BatchExecutor(fs=mock_fs)

        request = BatchRequest(
            operations=[
                WriteOp(path="/data.txt", content="hello"),
                ReadOp(path="/data.txt"),
            ],
        )
        response = await executor.execute(request, context=mock_context)
        assert len(response.results) == 2
        assert response.results[0].status == 201
        assert response.results[1].status == 200
        # Verify execution order
        assert execution_order == ["write:/data.txt", "read:/data.txt"]


# =============================================================================
# Test 8: Invalid operation type — caught by Pydantic discriminator
# =============================================================================


class TestInvalidOpType:
    def test_invalid_op_type_rejected(self) -> None:
        """Invalid operation type should fail Pydantic validation."""
        with pytest.raises(ValidationError):
            BatchRequest(
                operations=[{"op": "invalid", "path": "/test.txt"}],  # type: ignore[list-item]
            )


# =============================================================================
# Test 9: Write with base64 encoding
# =============================================================================


class TestBase64Encoding:
    @pytest.mark.asyncio()
    async def test_write_with_base64_encoding(
        self, executor: BatchExecutor, mock_fs: MagicMock, mock_context: MagicMock
    ) -> None:
        """Base64-encoded content should be decoded before writing."""
        import base64

        original = b"binary data \x00\x01\x02"
        encoded = base64.b64encode(original).decode("ascii")

        request = BatchRequest(
            operations=[WriteOp(path="/binary.dat", content=encoded, encoding="base64")],
        )
        response = await executor.execute(request, context=mock_context)
        assert response.results[0].status == 201
        # Verify the decoded bytes were passed to fs.write
        mock_fs.write.assert_called_once()
        call_kwargs = mock_fs.write.call_args
        assert (
            call_kwargs.kwargs.get("content") == original
            or call_kwargs[1].get("content") == original
        )


# =============================================================================
# Test 10: Per-operation timeout
# =============================================================================


class TestOperationTimeout:
    @pytest.mark.asyncio()
    async def test_slow_operation_times_out(
        self, mock_fs: MagicMock, mock_context: MagicMock
    ) -> None:
        """Operation exceeding timeout gets status 504."""
        import time

        def slow_read(path: str, **kwargs: Any) -> bytes:
            time.sleep(10)  # Way past the timeout
            return b"content"

        mock_fs.read.side_effect = slow_read
        executor = BatchExecutor(fs=mock_fs, operation_timeout=0.1)  # 100ms timeout

        request = BatchRequest(
            operations=[ReadOp(path="/slow.txt")],
        )
        response = await executor.execute(request, context=mock_context)
        assert response.results[0].status == 504
        assert response.results[0].error is not None
        assert "timed out" in response.results[0].error.lower()


# =============================================================================
# Test 11: Payload size validation
# =============================================================================


class TestPayloadSize:
    def test_payload_exceeds_10mb(self) -> None:
        """Total write content exceeding 10MB should fail validation."""
        # Create a write op with >10MB of content
        large_content = "x" * (10 * 1024 * 1024 + 1)  # 10MB + 1 byte
        with pytest.raises(ValidationError):
            BatchRequest(
                operations=[WriteOp(path="/large.txt", content=large_content)],
            )

    def test_payload_spread_across_ops_exceeds_10mb(self) -> None:
        """Multiple write ops whose total content exceeds 10MB should fail."""
        # 5 ops each with ~2.1MB = ~10.5MB total
        content = "x" * (2 * 1024 * 1024 + 100_000)
        with pytest.raises(ValidationError):
            BatchRequest(
                operations=[WriteOp(path=f"/file{i}.txt", content=content) for i in range(5)],
            )

    def test_payload_under_10mb_accepted(self) -> None:
        """Content under 10MB should be accepted."""
        content = "x" * (1024 * 1024)  # 1MB
        request = BatchRequest(
            operations=[WriteOp(path="/normal.txt", content=content)],
        )
        assert len(request.operations) == 1


# =============================================================================
# Test 12: Index-based result correlation
# =============================================================================


class TestIndexCorrelation:
    @pytest.mark.asyncio()
    async def test_results_have_correct_indices(
        self, executor: BatchExecutor, mock_context: MagicMock
    ) -> None:
        """Results should have indices matching their operation position."""
        request = BatchRequest(
            operations=[
                ReadOp(path="/a.txt"),
                WriteOp(path="/b.txt", content="data"),
                DeleteOp(path="/c.txt"),
            ],
        )
        response = await executor.execute(request, context=mock_context)
        for i, result in enumerate(response.results):
            assert result.index == i


# =============================================================================
# Test 13: Stat operation with missing file
# =============================================================================


class TestStatMissingFile:
    @pytest.mark.asyncio()
    async def test_stat_missing_file_returns_404(
        self, mock_fs: MagicMock, mock_context: MagicMock
    ) -> None:
        """Stat on a non-existent file should return 404."""
        mock_fs.get_metadata.return_value = None
        executor = BatchExecutor(fs=mock_fs)

        request = BatchRequest(
            operations=[StatOp(path="/missing.txt")],
        )
        response = await executor.execute(request, context=mock_context)
        assert response.results[0].status == 404


# =============================================================================
# Test 14: Error mapping — correct status codes for each exception type
# =============================================================================


class TestErrorMapping:
    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        ("exception", "expected_status"),
        [
            (NexusPermissionError("denied"), 403),
            (NexusFileNotFoundError(path="/x"), 404),
            (InvalidPathError("bad path"), 400),
            (FileExistsError("already exists"), 409),
            (RuntimeError("unexpected"), 500),
        ],
    )
    async def test_exception_to_status_mapping(
        self,
        mock_fs: MagicMock,
        mock_context: MagicMock,
        exception: Exception,
        expected_status: int,
    ) -> None:
        mock_fs.read.side_effect = exception
        executor = BatchExecutor(fs=mock_fs)

        request = BatchRequest(
            operations=[ReadOp(path="/test.txt")],
        )
        response = await executor.execute(request, context=mock_context)
        assert response.results[0].status == expected_status
