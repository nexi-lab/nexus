"""HTTP Range request utilities (RFC 9110 Section 14).

Provides parsing, validation, and response building for Range requests.
Used by all streaming endpoints to support partial content delivery,
download resumption, and media seeking.

Issue #790: HTTP Range Request Support.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from typing import Any

from starlette.responses import Response, StreamingResponse


class RangeNotSatisfiableError(Exception):
    """Raised when a Range header specifies an unsatisfiable range."""

    def __init__(self, total_size: int) -> None:
        self.total_size = total_size
        super().__init__(f"Range not satisfiable for resource of size {total_size}")


@dataclass(frozen=True)
class RangeSpec:
    """A single byte range with inclusive start/end (RFC 9110 semantics).

    Attributes:
        start: First byte position (inclusive, 0-based).
        end: Last byte position (inclusive, 0-based).
        total: Total size of the resource in bytes.
    """

    start: int
    end: int
    total: int

    @property
    def content_length(self) -> int:
        """Number of bytes in this range."""
        return self.end - self.start + 1

    @property
    def content_range_header(self) -> str:
        """Content-Range header value, e.g. 'bytes 0-499/1000'."""
        return f"bytes {self.start}-{self.end}/{self.total}"


def parse_range_header(
    range_header: str,
    total_size: int,
) -> list[RangeSpec] | None:
    """Parse an HTTP Range header into a list of RangeSpec objects.

    Args:
        range_header: Raw Range header value (e.g. "bytes=0-499").
        total_size: Total size of the resource in bytes.

    Returns:
        list[RangeSpec] for valid ranges, None for malformed/non-bytes headers.

    Raises:
        RangeNotSatisfiableError: If the range cannot be satisfied (416).
    """
    header = range_header.strip()
    if not header:
        return None

    # Must start with "bytes="
    match = re.match(r"bytes\s*=\s*(.*)", header, re.IGNORECASE)
    if not match:
        return None

    range_set = match.group(1).strip()
    if not range_set:
        return None

    _MAX_RANGES = 100  # Practical limit to prevent DoS via many ranges

    specs: list[RangeSpec] = []
    for part in range_set.split(","):
        if len(specs) >= _MAX_RANGES:
            return None  # Too many ranges → treat as malformed
        part = part.strip()
        if not part:
            continue

        spec = _parse_single_range(part, total_size)
        if spec is None:
            # Malformed individual range → treat entire header as malformed
            return None
        specs.append(spec)

    if not specs:
        return None

    return specs


def _parse_single_range(range_str: str, total_size: int) -> RangeSpec | None:
    """Parse a single range spec like '0-499', '500-', or '-500'.

    Returns None for malformed, raises RangeNotSatisfiableError for unsatisfiable.
    """
    match = re.match(r"^\s*(\d*)\s*-\s*(\d*)\s*$", range_str)
    if not match:
        return None

    start_str = match.group(1)
    end_str = match.group(2)

    if not start_str and not end_str:
        return None  # bare "-" → malformed

    # Suffix range: "-N" (last N bytes)
    if not start_str:
        suffix_length = int(end_str)
        if suffix_length == 0:
            return None  # bytes=-0 is semantically empty
        if total_size == 0:
            raise RangeNotSatisfiableError(total_size)
        start = max(0, total_size - suffix_length)
        return RangeSpec(start=start, end=total_size - 1, total=total_size)

    # Standard range: "N-M" or "N-" (open-ended)
    start = int(start_str)
    if total_size == 0 or start >= total_size:
        raise RangeNotSatisfiableError(total_size)

    end = min(int(end_str), total_size - 1) if end_str else total_size - 1

    # Validate start <= end (e.g. "bytes=5-3" is malformed)
    if start > end:
        return None

    return RangeSpec(start=start, end=end, total=total_size)


def check_if_range(
    if_range: str | None,
    etag: str | None,
) -> bool:
    """Check whether a Range request should be honored given If-Range.

    Only ETag-based If-Range is supported. Date-based If-Range (RFC 9110
    §13.1.5) will cause a mismatch, falling back to a full 200 response,
    which is allowed by the RFC.

    Args:
        if_range: If-Range header value (quoted ETag string), or None.
        etag: Server's current ETag for the resource (unquoted), or None.

    Returns:
        True if Range should be honored, False if Range should be ignored.
    """
    if if_range is None:
        return True  # No condition → honor Range

    if etag is None:
        return False  # Can't validate → ignore Range

    # Reject weak ETags per RFC 9110 §13.1.2
    if if_range.startswith("W/"):
        return False

    # Strip quotes for comparison
    client_etag = if_range.strip('"')
    return client_etag == etag


# Type alias for generator factories
ContentGenerator = Callable[[int, int, int], Iterator[bytes] | AsyncIterator[bytes]]
FullGenerator = Callable[[], Iterator[bytes] | AsyncIterator[bytes]]


def build_range_response(
    *,
    request_headers: Any,
    content_generator: ContentGenerator,
    full_generator: FullGenerator,
    total_size: int,
    etag: str | None,
    content_type: str,
    filename: str,
    extra_headers: dict[str, str] | None = None,
) -> Response | StreamingResponse:
    """Build an HTTP response handling Range requests.

    Decides between 200 (full), 206 (partial), or 416 (not satisfiable)
    based on request headers and resource metadata.

    Args:
        request_headers: Request headers (must support .get(key, default)).
        content_generator: Factory(start, end, chunk_size) → Iterator/AsyncIterator.
        full_generator: Factory() → Iterator/AsyncIterator for full content.
        total_size: Total size of the resource in bytes.
        etag: Server ETag (unquoted), or None.
        content_type: MIME type for the response.
        filename: Filename for Content-Disposition header.
        extra_headers: Additional headers to include in the response.

    Returns:
        Response (416) or StreamingResponse (200/206).
    """
    safe_filename = re.sub(r'["\\\r\n]', "_", filename) or "download"
    base_headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'attachment; filename="{safe_filename}"',
    }
    if etag is not None:
        base_headers["ETag"] = f'"{etag}"'
    if extra_headers:
        base_headers.update(extra_headers)

    range_header = request_headers.get("range")

    # No Range header → 200 full content
    if not range_header:
        return _full_response(full_generator, total_size, content_type, base_headers)

    # Parse Range header
    try:
        ranges = parse_range_header(range_header, total_size)
    except RangeNotSatisfiableError:
        return _416_response(total_size, base_headers)

    # Malformed → 200 (ignore Range per RFC)
    if ranges is None:
        return _full_response(full_generator, total_size, content_type, base_headers)

    # Multi-range → 200 (not implemented, serve full)
    if len(ranges) > 1:
        return _full_response(full_generator, total_size, content_type, base_headers)

    # Check If-Range
    if_range = request_headers.get("if-range")
    if not check_if_range(if_range, etag):
        return _full_response(full_generator, total_size, content_type, base_headers)

    # Single valid range → 206
    spec = ranges[0]
    range_headers = {
        **base_headers,
        "Content-Range": spec.content_range_header,
        "Content-Length": str(spec.content_length),
    }

    return StreamingResponse(
        content_generator(spec.start, spec.end, 8192),
        status_code=206,
        media_type=content_type,
        headers=range_headers,
    )


def _full_response(
    full_generator: FullGenerator,
    total_size: int,
    content_type: str,
    headers: dict[str, str],
) -> StreamingResponse:
    """Build a 200 full-content streaming response."""
    return StreamingResponse(
        full_generator(),
        status_code=200,
        media_type=content_type,
        headers={**headers, "Content-Length": str(total_size)},
    )


def _416_response(total_size: int, headers: dict[str, str]) -> Response:
    """Build a 416 Range Not Satisfiable response."""
    return Response(
        content=b"Range Not Satisfiable",
        status_code=416,
        media_type="text/plain",
        headers={**headers, "Content-Range": f"bytes */{total_size}"},
    )
