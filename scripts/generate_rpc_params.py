#!/usr/bin/env python3
"""Generate RPC Param dataclasses from @rpc_expose method signatures.

Usage:
    python scripts/generate_rpc_params.py          # Generate _rpc_params_generated.py
    python scripts/generate_rpc_params.py --check   # Exit 1 if file would change (CI mode)

This script introspects all @rpc_expose-decorated methods on NexusFS and its
service mixins, then generates corresponding @dataclass Param classes and the
METHOD_PARAMS mapping dict.

Manual overrides (ReadParams, OAuthGetAuthUrlParams, OAuthExchangeCodeParams,
and ReBAC tuple-field classes) live in ``_rpc_param_overrides.py`` and are
imported *after* the generated module so they replace the generated versions.
"""

from __future__ import annotations

import importlib
import inspect
import re
import sys
import textwrap
from pathlib import Path
from typing import Any, get_args, get_origin

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Output path for the generated file
OUTPUT_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "nexus" / "server" / "_rpc_params_generated.py"
)


# Methods whose Param classes are hand-written in _rpc_param_overrides.py.
# The codegen skips these entirely so the override takes precedence.
# Auto-populated from OVERRIDE_METHOD_PARAMS keys at generation time.
def _load_override_methods() -> set[str]:
    """Load method names from _rpc_param_overrides.OVERRIDE_METHOD_PARAMS."""
    try:
        from nexus.server._rpc_param_overrides import OVERRIDE_METHOD_PARAMS

        return set(OVERRIDE_METHOD_PARAMS.keys())
    except ImportError:
        # Fallback if overrides module not importable
        return {
            "sys_read",
            "oauth_get_auth_url",
            "oauth_exchange_code",
        }


OVERRIDE_METHODS: set[str] = _load_override_methods()

# Methods that should be excluded from codegen entirely (streaming, internal, etc.)
EXCLUDED_METHODS: set[str] = {
    "read_range",
    "stream",
    "stream_range",
    "write_stream",
    "export_metadata",
    "import_metadata",
    "batch_get_content_ids",
    "load_workspace_memory_config",
    "set_rebac_option",
    "get_rebac_option",
    "register_namespace",
    "get_namespace",
    "rebac_expand_with_privacy",
    "grant_consent",
    "revoke_consent",
    "rebac_check_batch",
    "share_with_group",
    "get_dynamic_viewer_config",
    "read_with_dynamic_viewer",
    "apply_dynamic_viewer_filter",
    # Codex review of #3701 (finding #3): semantic_search REMAINS exposed
    # via SearchService and is called positionally by RemoteServiceProxy
    # (e.g. ``proxy.semantic_search("needle", path="/docs", limit=5)``).
    # Excluding it from METHOD_PARAMS broke that positional binding —
    # restored so RPCProxyBase._get_param_names() can resolve "query".
    "semantic_search_stats",
    "initialize_semantic_search",
    "wait_for_changes",
    "lock",
    "extend_lock",
    "unlock",
}

# Modules to scan for @rpc_expose methods.  We inspect each class in each
# module and collect all methods that have ``_rpc_exposed = True``.
#
# Codex review of #3701 (finding #3): the previous list referenced stale
# ``nexus.system_services.*`` module paths that were moved during the
# kernel/services refactor. Those modules silently ImportError'd, the
# codegen logged a warning, and the resulting METHOD_PARAMS was missing
# entries for ``register_workspace``, ``register_agent``, and several
# other still-exposed RPCs. RemoteServiceProxy / RPCProxyBase rely on
# METHOD_PARAMS to map positional arguments to keyword names, so any
# missing entry silently misserializes positional remote calls. Fix by
# pointing at the canonical post-refactor locations.
MODULES_TO_SCAN: list[str] = [
    "nexus.core.nexus_fs",
    # Search (moved to bricks tier, Issue #1287)
    "nexus.bricks.search.search_service",
    # Services (reorganized into sub-packages)
    "nexus.bricks.share_link.share_link_service",
    "nexus.bricks.versioning.version_service",
    "nexus.bricks.mount.mount_service",
    # OAuth credential service (moved into bricks/auth/oauth/)
    "nexus.bricks.auth.oauth.credential_service",
    # post-refactor canonical locations (was nexus.system_services.*)
    "nexus.services.workspace.workspace_rpc_service",
    "nexus.services.agents.agent_rpc_service",
    "nexus.services.lifecycle.user_provisioning",
    "nexus.services.metadata_export",
    # Brick services with @rpc_expose
    "nexus.bricks.mcp.mcp_service",
    "nexus.bricks.rebac.rebac_service",
    # Server-side RPC services (Issue #2986: post-refactor split)
    "nexus.server.rpc.services.snapshots_rpc",
    "nexus.server.rpc.services.pay_rpc",
    "nexus.server.rpc.services.governance_rpc",
    "nexus.server.rpc.services.federation_rpc",
    "nexus.server.rpc.services.events_rpc",
    "nexus.server.rpc.services.audit_rpc",
]

# Types that signal "operation context" — parameters with these types are
# excluded from the generated Param class.
CONTEXT_TYPES: set[str] = {
    "OperationContext",
    "OperationContext | None",
    "Optional[OperationContext]",
}

# Params named _context or context that have OperationContext type are excluded.
# But params named context with dict type are INCLUDED (user-provided context).

# Type string rewrites for JSON compatibility
TYPE_REWRITES: dict[str, str] = {
    "datetime": "str",
    "datetime | None": "str | None",
    "timedelta": "str",
    "timedelta | None": "str | None",
    "Path": "str",
    "Path | None": "str | None",
    "Iterator[bytes]": "list[bytes]",
}

# Regex patterns for types that should be simplified to str (e.g. Literal['a', 'b'] → str)
LITERAL_PATTERN = re.compile(r"^Literal\[.*\]$")

# Type names that are not JSON-serializable and should be excluded from Param fields.
# Parameters with these types are omitted from the generated dataclass.
NON_SERIALIZABLE_TYPES: set[str] = {
    "ProgressCallback",
    "ProgressCallback | None",
}

# Regex patterns for non-serializable types (checked after _annotation_str).
# Catches resolved Callable types that don't match exact string entries above.
NON_SERIALIZABLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Callable"),
]

# Fields typed as tuple that need __post_init__ list→tuple conversion
TUPLE_FIELD_PATTERN = re.compile(r"tuple\[")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _annotation_str(annotation: Any) -> str:
    """Convert an annotation object to a clean string representation."""
    if annotation is inspect.Parameter.empty:
        return "Any"

    # Handle string annotations (from __future__ annotations)
    if isinstance(annotation, str):
        # Clean up string annotations that may contain Literal or builtins
        result = annotation.replace("builtins.", "")
        # Simplify Literal types to str for JSON compatibility
        if LITERAL_PATTERN.match(result):
            return "str"
        return result

    # Handle NoneType → "None" (before get_origin, which returns None for NoneType)
    if annotation is type(None):
        return "None"

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Handle None / NoneType in unions (fallback)
    if origin is type(None):
        return "None"

    # Handle Literal types → simplify to str
    import typing

    if origin is typing.Literal:
        return "str"

    # Handle Union types (X | Y)
    import types

    if origin is types.UnionType or (
        origin is not None and getattr(origin, "__name__", "") == "Union"
    ):
        parts = [_annotation_str(a) for a in args]
        return " | ".join(parts)

    # Handle basic generics
    if origin is not None and args:
        origin_name = getattr(origin, "__name__", str(origin))
        # Fix builtins prefix
        origin_name = origin_name.replace("builtins.", "")
        arg_strs = ", ".join(_annotation_str(a) for a in args)
        return f"{origin_name}[{arg_strs}]"

    # Handle simple types
    name = getattr(annotation, "__name__", None)
    if name:
        return name

    return str(annotation).replace("typing.", "").replace("builtins.", "")


def _default_repr(default: Any) -> str:
    """Convert a default value to its repr for code generation.

    Uses double-quoted strings to match ruff format conventions.
    """
    if default is inspect.Parameter.empty:
        return ""
    if default is None:
        return "None"
    if isinstance(default, str):
        # Use double quotes to match ruff format
        return f'"{default}"'
    if isinstance(default, bool):
        return repr(default)
    if isinstance(default, int | float):
        return repr(default)
    if isinstance(default, list | tuple | dict):
        return repr(default)
    return repr(default)


def _is_context_param(name: str, annotation_str: str) -> bool:
    """Return True if this parameter represents an OperationContext.

    Handles bare forms (``OperationContext``, ``OperationContext | None``)
    AND single-quoted double-forward-reference forms
    (``'OperationContext | None'``) that leak through when the source
    has ``context: "OperationContext | None"`` with `from __future__
    import annotations` — inspect.signature returns the annotation as
    a string that still contains the literal inner quotes.
    """
    # Strip any outer single/double quote pair before comparing — this
    # catches the double-forward-reference case.
    stripped = annotation_str.strip()
    if (stripped.startswith("'") and stripped.endswith("'")) or (
        stripped.startswith('"') and stripped.endswith('"')
    ):
        stripped = stripped[1:-1]
    if stripped in CONTEXT_TYPES or annotation_str in CONTEXT_TYPES:
        return True
    # Also exclude _context params (internal convention)
    return name.startswith("_") and "context" in name.lower()


def _is_private_param(name: str) -> bool:
    """Return True if this is a private/internal param (underscore-prefixed)."""
    return name.startswith("_")


def discover_rpc_methods() -> dict[str, tuple[str, inspect.Signature, str | None]]:
    """Discover all @rpc_expose methods across configured modules.

    Returns:
        dict mapping rpc_name → (class_name, signature, docstring).
        When duplicate rpc_names are found, the LAST one wins (NexusFS
        overrides service methods).
    """
    methods: dict[str, tuple[str, inspect.Signature, str | None]] = {}

    for module_path in MODULES_TO_SCAN:
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            print(f"WARNING: Could not import {module_path}: {e}", file=sys.stderr)
            continue

        for cls_name, cls in inspect.getmembers(module, inspect.isclass):
            # Only inspect classes defined in this module
            if cls.__module__ != module_path:
                continue

            for method_name in dir(cls):
                if method_name.startswith("__"):
                    continue
                try:
                    method = getattr(cls, method_name)
                except Exception:
                    continue

                if not callable(method) or not getattr(method, "_rpc_exposed", False):
                    continue

                rpc_name = getattr(method, "_rpc_name", method_name)

                # Get the actual function (unwrap classmethod/staticmethod)
                func = method
                if hasattr(func, "__func__"):
                    func = func.__func__

                try:
                    sig = inspect.signature(func, follow_wrapped=True)
                except (ValueError, TypeError):
                    continue

                docstring = inspect.getdoc(func)
                methods[rpc_name] = (cls_name, sig, docstring)

    return methods


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------


ACRONYM_MAP: dict[str, str] = {
    "mcp": "MCP",
    "oauth": "OAuth",
    "ace": "Ace",
    "rebac": "Rebac",
    "api": "API",
    "url": "URL",
    "gc": "GC",
}


def _method_to_class_name(method_name: str) -> str:
    """Convert method_name to PascalCase + 'Params'.

    e.g. 'read_bulk' → 'ReadBulkParams', 'mcp_connect' → 'MCPConnectParams'
    """
    parts = method_name.split("_")
    result_parts = []
    for p in parts:
        if p in ACRONYM_MAP:
            result_parts.append(ACRONYM_MAP[p])
        else:
            result_parts.append(p.capitalize())
    return "".join(result_parts) + "Params"


def _generate_param_class(
    rpc_name: str,
    class_name: str,
    sig: inspect.Signature,
    docstring: str | None,
) -> tuple[str, list[str], bool]:
    """Generate a @dataclass class definition for one RPC method.

    Returns:
        (class_source, list_of_required_fields, has_tuple_fields)
    """
    lines: list[str] = []
    required_fields: list[str] = []
    optional_fields: list[str] = []
    has_tuple_fields = False

    for param_name, param in sig.parameters.items():
        if param_name == "self":
            continue

        ann_str = _annotation_str(param.annotation)

        # Exclude OperationContext parameters
        if _is_context_param(param_name, ann_str):
            continue

        # Exclude private/internal params (underscore-prefixed)
        if _is_private_param(param_name):
            continue

        # Exclude non-serializable types (callbacks, etc.)
        if ann_str in NON_SERIALIZABLE_TYPES or any(
            p.search(ann_str) for p in NON_SERIALIZABLE_PATTERNS
        ):
            continue

        # Apply type rewrites
        ann_str = TYPE_REWRITES.get(ann_str, ann_str)

        # Check for tuple fields
        if TUPLE_FIELD_PATTERN.search(ann_str):
            has_tuple_fields = True

        if param.default is inspect.Parameter.empty:
            required_fields.append(f"    {param_name}: {ann_str}")
        else:
            default_str = _default_repr(param.default)
            optional_fields.append(f"    {param_name}: {ann_str} = {default_str}")

    # Build the class
    doc_line = f'    """Parameters for {rpc_name}() method."""'
    if docstring:
        first_line = docstring.split("\n")[0].strip()
        if first_line:
            doc_line = f'    """Parameters for {rpc_name}(): {first_line}"""'

    lines.append("@dataclass")
    lines.append(f"class {class_name}:")
    lines.append(doc_line)
    lines.append("")

    all_fields = required_fields + optional_fields
    if not all_fields:
        lines.append("    pass")
    else:
        lines.extend(all_fields)

    # Add __post_init__ for tuple fields (list→tuple conversion from JSON)
    if has_tuple_fields:
        lines.append("")
        lines.append("    def __post_init__(self) -> None:")
        lines.append('        """Convert lists to tuples (JSON deserializes tuples as lists)."""')
        for field_line in required_fields + optional_fields:
            # Parse field name and type
            match = re.match(r"\s+(\w+):\s+(.+?)(?:\s*=.*)?$", field_line)
            if match:
                fname = match.group(1)
                ftype = match.group(2)
                if TUPLE_FIELD_PATTERN.search(ftype):
                    lines.append(f"        if isinstance(self.{fname}, list):")
                    lines.append(
                        f'            object.__setattr__(self, "{fname}", tuple(self.{fname}))'
                    )

    return "\n".join(lines), [f.strip().split(":")[0] for f in required_fields], has_tuple_fields


def generate_file(methods: dict[str, tuple[str, inspect.Signature, str | None]]) -> str:
    """Generate the full _rpc_params_generated.py file content."""
    header = textwrap.dedent('''\
        """Auto-generated RPC Param dataclasses — DO NOT EDIT MANUALLY.

        Generated by ``scripts/generate_rpc_params.py`` from @rpc_expose method
        signatures.  Re-run that script whenever you add or change an RPC method.

        Manual overrides live in ``_rpc_param_overrides.py`` and are imported
        after this module by ``protocol.py``.
        """

        from __future__ import annotations

        from dataclasses import dataclass
        from typing import Any

    ''')

    class_blocks: list[str] = []
    method_params_entries: list[str] = []
    generated_class_names: list[str] = []

    # Sort methods alphabetically for stable output
    for rpc_name in sorted(methods.keys()):
        if rpc_name in OVERRIDE_METHODS:
            continue
        if rpc_name in EXCLUDED_METHODS:
            continue

        class_name = _method_to_class_name(rpc_name)
        _, sig, docstring = methods[rpc_name]

        class_source, _, _ = _generate_param_class(rpc_name, class_name, sig, docstring)
        class_blocks.append(class_source)
        method_params_entries.append(f'    "{rpc_name}": {class_name},')
        generated_class_names.append(class_name)

    # Build METHOD_PARAMS dict
    method_params_block = "METHOD_PARAMS: dict[str, type] = {\n"
    method_params_block += "\n".join(method_params_entries)
    method_params_block += "\n}\n"

    # Build __all__
    all_names = sorted(generated_class_names)
    all_block = "__all__ = [\n"
    for name in all_names:
        all_block += f'    "{name}",\n'
    all_block += "]\n"

    # Combine everything
    content = header
    content += all_block
    content += "\n\n"
    content += "\n\n\n".join(class_blocks)
    content += "\n\n\n"
    content += method_params_block

    return content


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    check_mode = "--check" in sys.argv

    # Ensure the project root is on sys.path so we can import nexus
    project_root = Path(__file__).resolve().parent.parent
    src_dir = project_root / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    print("Discovering @rpc_expose methods...", file=sys.stderr)
    methods = discover_rpc_methods()
    print(f"Found {len(methods)} exposed methods", file=sys.stderr)

    # Filter to only methods in current METHOD_PARAMS or valid new ones
    eligible = {k: v for k, v in methods.items() if k not in EXCLUDED_METHODS}
    print(f"Eligible for codegen: {len(eligible)} methods", file=sys.stderr)

    content = generate_file(eligible)

    if check_mode:
        if OUTPUT_PATH.exists():
            existing = OUTPUT_PATH.read_text()
            if existing == content:
                print("OK: Generated file is up-to-date", file=sys.stderr)
                return 0
            else:
                print(
                    f"FAIL: {OUTPUT_PATH} is stale. Run `python scripts/generate_rpc_params.py` to update.",
                    file=sys.stderr,
                )
                return 1
        else:
            print(f"FAIL: {OUTPUT_PATH} does not exist.", file=sys.stderr)
            return 1

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(content)
    print(f"Wrote {OUTPUT_PATH} ({len(content)} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
