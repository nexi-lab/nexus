"""Extract generic gRPC `Call` names from a dispatch dict literal via AST.

The dispatch lives in src/nexus/server/_kernel_syscall_dispatch.py as a
module-level dict assignment. Keys are string literals (the Call names).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RawGrpcCallName:
    name: str
    source: str  # "file.py:line"


def extract_grpc_call_names(
    py_path: Path,
    *,
    dispatch_var: str = "DISPATCH",
) -> list[RawGrpcCallName]:
    tree = ast.parse(py_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == dispatch_var:
                    return _names_from_dict(node.value, py_path)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == dispatch_var
            and node.value is not None
        ):
            return _names_from_dict(node.value, py_path)
    raise ValueError(f"variable {dispatch_var!r} not found in {py_path}")


def _names_from_dict(value: ast.AST, py_path: Path) -> list[RawGrpcCallName]:
    if not isinstance(value, ast.Dict):
        raise ValueError("dispatch value must be a dict literal")
    out: list[RawGrpcCallName] = []
    for k in value.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            out.append(
                RawGrpcCallName(
                    name=k.value,
                    source=f"{py_path}:{k.lineno}",
                )
            )
    return sorted(out, key=lambda r: r.name)
