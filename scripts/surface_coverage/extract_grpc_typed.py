"""Extract typed gRPC methods from .proto files via regex.

Recognizes the proto3 `service Foo { rpc Bar (...) returns (...); ... }` block.
Multi-service files supported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SERVICE_BLOCK_RE = re.compile(
    r"\bservice\s+(?P<service>[A-Za-z_]\w*)\s*\{(?P<body>.*?)\}",
    re.DOTALL,
)
_RPC_RE = re.compile(r"\brpc\s+(?P<method>[A-Za-z_]\w*)\s*\(")


@dataclass(frozen=True)
class RawGrpcTypedMethod:
    method: str  # "<Service>.<Method>"
    source: str  # "file.proto:line"


def extract_grpc_typed_methods(proto_path: Path) -> list[RawGrpcTypedMethod]:
    text = proto_path.read_text()
    out: list[RawGrpcTypedMethod] = []
    for block in _SERVICE_BLOCK_RE.finditer(text):
        service = block.group("service")
        body = block.group("body")
        body_start_offset = block.start("body")
        for rpc in _RPC_RE.finditer(body):
            absolute_offset = body_start_offset + rpc.start()
            line = text.count("\n", 0, absolute_offset) + 1
            out.append(
                RawGrpcTypedMethod(
                    method=f"{service}.{rpc.group('method')}",
                    source=f"{proto_path}:{line}",
                )
            )
    return sorted(out, key=lambda r: r.method)
