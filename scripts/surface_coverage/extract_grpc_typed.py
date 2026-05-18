"""Extract typed gRPC methods from .proto files.

Recognizes proto3 `service Foo { rpc Bar (...) returns (...); ... }` blocks.
Multi-service files supported. Uses a brace-aware scanner that ignores braces
inside line/block comments and string literals so paths like `/{title}` in
documentation don't terminate the service body prematurely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SERVICE_HEADER_RE = re.compile(r"\bservice\s+(?P<service>[A-Za-z_]\w*)\s*\{")
_RPC_RE = re.compile(r"\brpc\s+(?P<method>[A-Za-z_]\w*)\s*\(")


@dataclass(frozen=True)
class RawGrpcTypedMethod:
    method: str  # "<Service>.<Method>"
    source: str  # "file.proto:line"


def extract_grpc_typed_methods(proto_path: Path) -> list[RawGrpcTypedMethod]:
    raw_text = proto_path.read_text(encoding="utf-8")
    # Replace comments + string-literal contents with spaces so service/rpc
    # regexes can't match inside them. Offsets are preserved for line-number
    # reporting.
    text = _scrub_comments_and_strings(raw_text)
    out: list[RawGrpcTypedMethod] = []
    for header in _SERVICE_HEADER_RE.finditer(text):
        service = header.group("service")
        body_start = header.end()  # position right after the opening brace
        body_end = _find_matching_brace(text, body_start)
        if body_end is None:
            continue
        body = text[body_start:body_end]
        for rpc in _RPC_RE.finditer(body):
            absolute_offset = body_start + rpc.start()
            line = raw_text.count("\n", 0, absolute_offset) + 1
            out.append(
                RawGrpcTypedMethod(
                    method=f"{service}.{rpc.group('method')}",
                    source=f"{proto_path}:{line}",
                )
            )
    return sorted(out, key=lambda r: r.method)


def _scrub_comments_and_strings(text: str) -> str:
    """Return `text` with comment contents and string-literal contents replaced
    by spaces (newlines preserved). Length and line numbers unchanged."""
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            j = text.find("\n", i + 2)
            end = n if j == -1 else j
            for k in range(i, end):
                out[k] = " "
            i = end
            continue
        if ch == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            end = n if j == -1 else j + 2
            for k in range(i, end):
                if out[k] != "\n":
                    out[k] = " "
            i = end
            continue
        if ch in ('"', "'"):
            quote = ch
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    break
                j += 1
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def _find_matching_brace(text: str, start: int) -> int | None:
    """Return the offset of the `}` that matches the `{` opened just before `start`.

    Skips over braces that appear inside `// line comments`, `/* block comments */`,
    or string literals. Nested braces are tracked.
    """
    depth = 1
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        # Line comment
        if ch == "/" and nxt == "/":
            newline = text.find("\n", i + 2)
            i = n if newline == -1 else newline + 1
            continue
        # Block comment
        if ch == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                return None
            i = end + 2
            continue
        # String literal — proto3 supports both "..." and '...'
        if ch in ('"', "'"):
            quote = ch
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == quote:
                    break
                j += 1
            i = j + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None
