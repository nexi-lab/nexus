"""Extract public method names from SDK client classes via AST.

v3: walks src/nexus/remote/ recursively and discovers classes that look like
SDK clients (any class whose name ends in "Client", "Backend", "Metastore",
or matches an allowlist). Captures all public sync + async methods.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

# Class-name patterns that mark a class as part of the SDK surface
_SDK_CLASS_SUFFIXES = ("Client", "Backend", "Metastore", "Proxy", "Wrapper")


@dataclass(frozen=True)
class RawSdkMethod:
    class_name: str
    method_name: str
    source: str


def extract_sdk_methods(
    py_path_or_dir: Path,
    *,
    class_names: tuple[str, ...] = (),
) -> list[RawSdkMethod]:
    """Accept a single file (legacy) with explicit class_names, or a directory
    to walk and auto-discover SDK-like classes.
    """
    out: list[RawSdkMethod] = []
    if py_path_or_dir.is_file():
        out.extend(_extract_from_file(py_path_or_dir, class_names=class_names))
    elif py_path_or_dir.is_dir():
        for py in sorted(py_path_or_dir.rglob("*.py")):
            # skip tests
            if "/tests/" in str(py) or py.name.startswith("test_"):
                continue
            out.extend(_extract_from_file(py, class_names=()))
    # dedupe by (class_name, method_name) preferring earliest source
    seen: dict[tuple[str, str], RawSdkMethod] = {}
    for m in out:
        key = (m.class_name, m.method_name)
        if key not in seen:
            seen[key] = m
    return sorted(seen.values(), key=lambda r: (r.class_name, r.method_name))


def _extract_from_file(py_path: Path, *, class_names: tuple[str, ...]) -> list[RawSdkMethod]:
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    out: list[RawSdkMethod] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if class_names:
            if node.name not in class_names:
                continue
        elif not any(node.name.endswith(suf) for suf in _SDK_CLASS_SUFFIXES):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name.startswith("_"):
                continue
            out.append(
                RawSdkMethod(
                    class_name=node.name,
                    method_name=item.name,
                    source=f"{py_path}:{item.lineno}",
                )
            )
    return out
