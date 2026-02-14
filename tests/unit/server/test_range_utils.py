"""Unit tests for HTTP Range request utilities (Issue #790).

Tests cover:
- RangeSpec dataclass properties
- parse_range_header() with 25+ parametrized edge cases
- check_if_range() ETag comparison logic
- build_range_response() 200/206/416 response generation
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from unittest.mock import MagicMock

import pytest

from nexus.server.range_utils import (
    RangeNotSatisfiableError,
    RangeSpec,
    build_range_response,
    check_if_range,
    parse_range_header,
)

# =============================================================================
# RangeSpec
# =============================================================================


class TestRangeSpec:
    def test_content_length(self) -> None:
        spec = RangeSpec(start=0, end=499, total=1000)
        assert spec.content_length == 500

    def test_content_length_single_byte(self) -> None:
        spec = RangeSpec(start=0, end=0, total=100)
        assert spec.content_length == 1

    def test_content_range_header(self) -> None:
        spec = RangeSpec(start=0, end=499, total=1000)
        assert spec.content_range_header == "bytes 0-499/1000"

    def test_content_range_header_full_file(self) -> None:
        spec = RangeSpec(start=0, end=999, total=1000)
        assert spec.content_range_header == "bytes 0-999/1000"

    def test_immutable(self) -> None:
        spec = RangeSpec(start=0, end=499, total=1000)
        with pytest.raises(AttributeError):
            spec.start = 10  # type: ignore[misc]


# =============================================================================
# parse_range_header — parametrized
# =============================================================================


class TestParseRangeHeader:
    @pytest.mark.parametrize(
        "header, total, expected",
        [
            # Basic ranges
            ("bytes=0-499", 1000, [RangeSpec(0, 499, 1000)]),
            ("bytes=500-999", 1000, [RangeSpec(500, 999, 1000)]),
            ("bytes=0-0", 1000, [RangeSpec(0, 0, 1000)]),
            # Suffix range (last N bytes)
            ("bytes=-500", 1000, [RangeSpec(500, 999, 1000)]),
            ("bytes=-100", 1000, [RangeSpec(900, 999, 1000)]),
            ("bytes=-1", 1000, [RangeSpec(999, 999, 1000)]),
            ("bytes=-1000", 1000, [RangeSpec(0, 999, 1000)]),
            # Open-ended range (from start to end)
            ("bytes=500-", 1000, [RangeSpec(500, 999, 1000)]),
            ("bytes=0-", 1000, [RangeSpec(0, 999, 1000)]),
            ("bytes=999-", 1000, [RangeSpec(999, 999, 1000)]),
            # Clamping: end exceeds total
            ("bytes=0-9999", 1000, [RangeSpec(0, 999, 1000)]),
            ("bytes=500-2000", 1000, [RangeSpec(500, 999, 1000)]),
            # Multi-range (parser returns list, caller decides what to do)
            (
                "bytes=0-499, 500-999",
                1000,
                [RangeSpec(0, 499, 1000), RangeSpec(500, 999, 1000)],
            ),
            # Whitespace tolerance
            ("bytes=0-499 ", 1000, [RangeSpec(0, 499, 1000)]),
            (" bytes=0-499", 1000, [RangeSpec(0, 499, 1000)]),
            ("bytes= 0 - 499", 1000, [RangeSpec(0, 499, 1000)]),
            # Suffix range larger than file → clamp to full file
            ("bytes=-5000", 1000, [RangeSpec(0, 999, 1000)]),
            # Small files
            ("bytes=0-0", 1, [RangeSpec(0, 0, 1)]),
            ("bytes=-1", 1, [RangeSpec(0, 0, 1)]),
        ],
        ids=[
            "first-500",
            "second-500",
            "single-first-byte",
            "suffix-500",
            "suffix-100",
            "suffix-1",
            "suffix-exact",
            "from-500",
            "from-start",
            "last-byte-open",
            "clamp-end-past-total",
            "clamp-end-mid",
            "multi-range",
            "trailing-space",
            "leading-space",
            "internal-spaces",
            "suffix-exceeds",
            "single-byte-file",
            "suffix-single-byte-file",
        ],
    )
    def test_valid_ranges(
        self,
        header: str,
        total: int,
        expected: list[RangeSpec],
    ) -> None:
        result = parse_range_header(header, total)
        assert result == expected

    @pytest.mark.parametrize(
        "header, total",
        [
            ("", 1000),
            ("invalid", 1000),
            ("items=0-499", 1000),  # not bytes unit
            ("bytes=", 1000),
            ("bytes=abc-def", 1000),
            ("bytes=-", 1000),
            ("bytes=5-3", 1000),  # start > end
        ],
        ids=[
            "empty",
            "no-bytes-prefix",
            "wrong-unit",
            "no-range-spec",
            "non-numeric",
            "bare-dash",
            "start-gt-end",
        ],
    )
    def test_malformed_returns_none(self, header: str, total: int) -> None:
        """Malformed Range headers return None (caller serves 200)."""
        assert parse_range_header(header, total) is None

    @pytest.mark.parametrize(
        "header, total",
        [
            ("bytes=1000-1500", 1000),  # start beyond EOF
            ("bytes=1500-2000", 1000),  # both beyond EOF
        ],
        ids=[
            "start-at-eof",
            "both-past-eof",
        ],
    )
    def test_unsatisfiable_raises(self, header: str, total: int) -> None:
        """Unsatisfiable ranges raise RangeNotSatisfiableError."""
        with pytest.raises(RangeNotSatisfiableError) as exc_info:
            parse_range_header(header, total)
        assert exc_info.value.total_size == total

    def test_zero_size_file_any_range_is_unsatisfiable(self) -> None:
        with pytest.raises(RangeNotSatisfiableError):
            parse_range_header("bytes=0-0", 0)

    def test_suffix_zero_returns_none(self) -> None:
        """bytes=-0 is technically valid but semantically empty → None."""
        assert parse_range_header("bytes=-0", 1000) is None


# =============================================================================
# check_if_range
# =============================================================================


class TestCheckIfRange:
    def test_no_if_range_header(self) -> None:
        """No If-Range → honor Range."""
        assert check_if_range(None, "abc123") is True

    def test_matching_etag(self) -> None:
        """Matching ETag → honor Range."""
        assert check_if_range('"abc123"', "abc123") is True

    def test_mismatching_etag(self) -> None:
        """Non-matching ETag → ignore Range, serve 200."""
        assert check_if_range('"old-etag"', "new-etag") is False

    def test_weak_etag_rejected(self) -> None:
        """Weak ETags not valid for Range (RFC 9110 §13.1.2)."""
        assert check_if_range('W/"abc123"', "abc123") is False

    def test_no_server_etag(self) -> None:
        """No server ETag → cannot validate, ignore Range."""
        assert check_if_range('"abc123"', None) is False

    def test_both_none(self) -> None:
        """No If-Range and no ETag → honor Range (no condition)."""
        assert check_if_range(None, None) is True


# =============================================================================
# build_range_response
# =============================================================================


class TestBuildRangeResponse:
    """Test build_range_response with mock generators."""

    def _make_headers(
        self,
        range_header: str | None = None,
        if_range: str | None = None,
    ) -> MagicMock:
        headers = MagicMock()

        def get_header(key: str, default: str | None = None) -> str | None:
            mapping = {
                "range": range_header,
                "if-range": if_range,
            }
            return mapping.get(key.lower(), default)

        headers.get = get_header
        return headers

    def _sync_content_gen(self, data: bytes) -> Callable:
        """Create a sync content_generator(start, end, chunk_size)."""

        def gen(start: int, end: int, chunk_size: int = 8192) -> Iterator[bytes]:
            sliced = data[start : end + 1]
            for i in range(0, len(sliced), chunk_size):
                yield sliced[i : i + chunk_size]

        return gen

    def _sync_full_gen(self, data: bytes) -> Callable:
        """Create a sync full_generator()."""

        def gen() -> Iterator[bytes]:
            yield data

        return gen

    def test_no_range_header_returns_200(self) -> None:
        data = b"Hello World"
        headers = self._make_headers()
        resp = build_range_response(
            request_headers=headers,
            content_generator=self._sync_content_gen(data),
            full_generator=self._sync_full_gen(data),
            total_size=len(data),
            etag="abc123",
            content_type="text/plain",
            filename="test.txt",
        )
        assert resp.status_code == 200
        assert resp.headers.get("accept-ranges") == "bytes"
        assert resp.headers.get("content-length") == str(len(data))

    def test_valid_range_returns_206(self) -> None:
        data = b"0123456789"
        headers = self._make_headers(range_header="bytes=0-4")
        resp = build_range_response(
            request_headers=headers,
            content_generator=self._sync_content_gen(data),
            full_generator=self._sync_full_gen(data),
            total_size=len(data),
            etag="abc123",
            content_type="text/plain",
            filename="test.txt",
        )
        assert resp.status_code == 206
        assert resp.headers.get("content-range") == "bytes 0-4/10"
        assert resp.headers.get("content-length") == "5"
        assert resp.headers.get("accept-ranges") == "bytes"

    def test_suffix_range_returns_206(self) -> None:
        data = b"0123456789"
        headers = self._make_headers(range_header="bytes=-3")
        resp = build_range_response(
            request_headers=headers,
            content_generator=self._sync_content_gen(data),
            full_generator=self._sync_full_gen(data),
            total_size=len(data),
            etag="abc123",
            content_type="text/plain",
            filename="test.txt",
        )
        assert resp.status_code == 206
        assert resp.headers.get("content-range") == "bytes 7-9/10"

    def test_unsatisfiable_returns_416(self) -> None:
        data = b"0123456789"
        headers = self._make_headers(range_header="bytes=20-30")
        resp = build_range_response(
            request_headers=headers,
            content_generator=self._sync_content_gen(data),
            full_generator=self._sync_full_gen(data),
            total_size=len(data),
            etag="abc123",
            content_type="text/plain",
            filename="test.txt",
        )
        assert resp.status_code == 416
        assert resp.headers.get("content-range") == "bytes */10"

    def test_malformed_range_returns_200(self) -> None:
        data = b"Hello World"
        headers = self._make_headers(range_header="invalid")
        resp = build_range_response(
            request_headers=headers,
            content_generator=self._sync_content_gen(data),
            full_generator=self._sync_full_gen(data),
            total_size=len(data),
            etag="abc123",
            content_type="text/plain",
            filename="test.txt",
        )
        assert resp.status_code == 200

    def test_multi_range_falls_back_to_200(self) -> None:
        data = b"0123456789"
        headers = self._make_headers(range_header="bytes=0-4, 5-9")
        resp = build_range_response(
            request_headers=headers,
            content_generator=self._sync_content_gen(data),
            full_generator=self._sync_full_gen(data),
            total_size=len(data),
            etag="abc123",
            content_type="text/plain",
            filename="test.txt",
        )
        # Multi-range falls back to 200 (not implemented yet)
        assert resp.status_code == 200

    def test_if_range_match_returns_206(self) -> None:
        data = b"0123456789"
        headers = self._make_headers(range_header="bytes=0-4", if_range='"abc123"')
        resp = build_range_response(
            request_headers=headers,
            content_generator=self._sync_content_gen(data),
            full_generator=self._sync_full_gen(data),
            total_size=len(data),
            etag="abc123",
            content_type="text/plain",
            filename="test.txt",
        )
        assert resp.status_code == 206

    def test_if_range_mismatch_returns_200(self) -> None:
        data = b"0123456789"
        headers = self._make_headers(range_header="bytes=0-4", if_range='"stale-etag"')
        resp = build_range_response(
            request_headers=headers,
            content_generator=self._sync_content_gen(data),
            full_generator=self._sync_full_gen(data),
            total_size=len(data),
            etag="abc123",
            content_type="text/plain",
            filename="test.txt",
        )
        assert resp.status_code == 200

    def test_etag_in_response_header(self) -> None:
        data = b"Hello"
        headers = self._make_headers()
        resp = build_range_response(
            request_headers=headers,
            content_generator=self._sync_content_gen(data),
            full_generator=self._sync_full_gen(data),
            total_size=len(data),
            etag="abc123",
            content_type="text/plain",
            filename="test.txt",
        )
        assert resp.headers.get("etag") == '"abc123"'

    def test_content_disposition(self) -> None:
        data = b"Hello"
        headers = self._make_headers()
        resp = build_range_response(
            request_headers=headers,
            content_generator=self._sync_content_gen(data),
            full_generator=self._sync_full_gen(data),
            total_size=len(data),
            etag="abc123",
            content_type="text/plain",
            filename="test.txt",
        )
        assert 'filename="test.txt"' in resp.headers.get("content-disposition", "")

    def test_extra_headers_included(self) -> None:
        data = b"Hello"
        headers = self._make_headers()
        resp = build_range_response(
            request_headers=headers,
            content_generator=self._sync_content_gen(data),
            full_generator=self._sync_full_gen(data),
            total_size=len(data),
            etag="abc123",
            content_type="text/plain",
            filename="test.txt",
            extra_headers={"X-Custom": "value"},
        )
        assert resp.headers.get("x-custom") == "value"

    def test_no_etag(self) -> None:
        data = b"Hello"
        headers = self._make_headers(range_header="bytes=0-4")
        resp = build_range_response(
            request_headers=headers,
            content_generator=self._sync_content_gen(data),
            full_generator=self._sync_full_gen(data),
            total_size=len(data),
            etag=None,
            content_type="text/plain",
            filename="test.txt",
        )
        assert resp.status_code == 206
        assert "etag" not in resp.headers


class TestRangeNotSatisfiableError:
    def test_attributes(self) -> None:
        err = RangeNotSatisfiableError(total_size=1000)
        assert err.total_size == 1000
        assert "1000" in str(err)
