#!/usr/bin/env python3
"""codegen_kernel_abi.py — Generate Python artifacts from Rust kernel definitions.

ONE script reads Rust trait/pyclass/pyfunction definitions, generates ALL
multi-language artifacts.  Zero hand-written glue.

Usage:
    python scripts/codegen_kernel_abi.py           # Generate all files
    python scripts/codegen_kernel_abi.py --check   # Verify files are up-to-date (CI)

Directions:
    1. SYSCALL  — Kernel provides → clients call
       Output: .pyi stubs, kernel_exports.py
    2. DISPATCH — Kernel calls → users provide
       Output: Python Protocol classes for hook/resolver/observer authors
    3. PILLAR   — Kernel calls → storage provides
       Output: Python Protocol class for metastore (future: .proto)
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass, field
from itertools import zip_longest
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
RUST_SRC = ROOT / "rust" / "kernel" / "src"

STUBS_PATH = ROOT / "stubs" / "nexus_runtime" / "__init__.pyi"
EXPORTS_PATH = ROOT / "src" / "nexus" / "core" / "kernel_exports.py"
PROTOCOLS_PATH = ROOT / "src" / "nexus" / "core" / "kernel_protocols.py"
API_GROUPS_PATH = ROOT / "src" / "nexus" / "_kernel_api_groups.py"
# Thin RPC dispatch — gRPC method name → kernel syscall.  The codegen
# scans PyKernel #[pymethods] (Rust SSOT) and emits the Python module
# the gRPC servicer's `Call` handler consults BEFORE the legacy
# dispatch_method path.  Replaces the multi-layer chain (dispatch.py
# table + handlers/filesystem.py wrappers + _rpc_params_generated.py
# dataclasses + _rpc_param_overrides.py manual fixups) for syscalls.
KERNEL_DISPATCH_PATH = ROOT / "src" / "nexus" / "server" / "_kernel_syscall_dispatch.py"
# Single PyO3 transport file. Phase C renamed from `generated_pyo3.rs` to
# the more explicit `generated_kernel_abi_pyo3.rs` so a future codegen
# generated_lib_abi_pyo3.rs (Phase H — `lib::python::*` wrappers) can
# coexist without filename collision.
GENERATED_PYO3_PATH = RUST_SRC / "generated_kernel_abi_pyo3.rs"

# Phase C compat: lib.rs still references modules under their flat
# pre-Phase-C names via `pub use core::… as <flat_name>` re-exports.
# Map the flat name → on-disk file for the codegen scanner.
FLAT_TO_NESTED_ALIASES: dict[str, str] = {
    # Phase C: kernel-internal modules nested under core/.
    "lock_manager": "core/lock/mod.rs",
    "locks": "core/lock/locks.rs",
    "semaphore": "core/lock/semaphore.rs",
    "dispatch": "core/dispatch/mod.rs",
    "hook_registry": "core/dispatch/hook_registry.rs",
    # Phase 0.5 — Rust trait Metastore renamed MetaStore for §3 pillar
    # symmetry with `ObjectStore` / `CacheStore`. Directory renamed
    # `core/metastore/` → `core/meta_store/`. Flat alias keys keep their
    # historical `metastore` / `remote_metastore` names because lib.rs's
    # compat re-exports still surface the modules under those Rust paths
    # for callers that haven't migrated yet.
    "metastore": "core/meta_store/mod.rs",
    "remote_metastore": "core/meta_store/remote.rs",
    "meta_store": "core/meta_store/mod.rs",
    "remote_meta_store": "core/meta_store/remote.rs",
    "pipe": "core/pipe/mod.rs",
    "pipe_manager": "core/pipe/manager.rs",
    "stdio_pipe": "core/pipe/stdio.rs",
    "remote_pipe": "core/pipe/remote.rs",
    "stream": "core/stream/mod.rs",
    "stream_manager": "core/stream/manager.rs",
    "stream_observer": "core/stream/observer.rs",
    "remote_stream": "core/stream/remote.rs",
    "wal_stream": "core/stream/wal.rs",
    "vfs_router": "core/vfs_router.rs",
    "dlc": "core/dlc.rs",
    "service_registry": "core/service_registry.rs",
    "file_watch": "core/file_watch.rs",
    # Phase G: kernel.rs split — moved to kernel/mod.rs as the entry
    # point.  Per-syscall-family submodules (kernel/io.rs, mount.rs,
    # dispatch.rs, ipc.rs, federation.rs, agents.rs, locks.rs,
    # observability.rs) live alongside.
    "kernel": "kernel/mod.rs",
}

# PyO3 wrappers for the lib algorithms live in
# rust/lib/src/python/. The codegen uses flat module names
# (rebac, search, glob, io, prefix, simd, trigram, path_utils, bitmap,
# bloom, hash) in the registration calls; this map locates the actual
# .rs file for each.
LIB_PYTHON_DIR = ROOT / "rust" / "lib" / "src" / "python"
LIB_PYTHON_MODULES: set[str] = {
    "bitmap",
    "bloom",
    "glob",
    "hash",
    "io",
    "path_utils",
    "prefix",
    "rebac",
    "search",
    "simd",
    "trigram",
}


def _resolve_module_path(mod_name: str) -> Path | None:
    """Return the on-disk `.rs` file for a flat module name, or None.

    Resolution order:
      1. `rust/lib/src/python/<mod>.rs` for algorithm wrappers under lib.
      2. Nested file aliases in the kernel `core/*` tree.
      3. Flat `rust/kernel/src/<mod>.rs`.
      4. Peer crates' `src/<mod>.rs` — `transport::python::register` adds
         `add_class::<grpc::PyVfsGrpcServerHandle>` etc.; the `grpc` segment
         resolves to `rust/transport/src/grpc.rs`.  Same shape for
         `federation`, `backends/`, `services/`.
    """
    if mod_name in LIB_PYTHON_MODULES:
        candidate = LIB_PYTHON_DIR / f"{mod_name}.rs"
        if candidate.exists():
            return candidate
    aliased = FLAT_TO_NESTED_ALIASES.get(mod_name)
    if aliased is not None:
        candidate = RUST_SRC / aliased
        if candidate.exists():
            return candidate
    flat = RUST_SRC / f"{mod_name}.rs"
    if flat.exists():
        return flat
    for peer in ("transport", "backends", "services"):
        peer_root = ROOT / "rust" / peer / "src"
        peer_flat = peer_root / f"{mod_name}.rs"
        if peer_flat.exists():
            return peer_flat
        peer_nested = peer_root / mod_name / "mod.rs"
        if peer_nested.exists():
            return peer_nested
        # Sub-module: `python/grpc_bridge.rs` registered via
        # `grpc_bridge::PyVfsGrpcServerHandle` in python/mod.rs.
        peer_python = peer_root / "python" / f"{mod_name}.rs"
        if peer_python.exists():
            return peer_python
    return None


KERNEL_RPC_HANDLERS_PATH = (
    ROOT / "src" / "nexus" / "server" / "rpc" / "handlers" / "_kernel_lock.py"
)

MARKER = "# AUTO-GENERATED by scripts/codegen_kernel_abi.py — DO NOT EDIT"
RUST_MARKER = "// AUTO-GENERATED by scripts/codegen_kernel_abi.py — DO NOT EDIT"

# ── Data Model ─────────────────────────────────────────────────────


@dataclass
class Param:
    name: str
    py_type: str
    default: str | None = None
    rust_type: str = ""  # Original Rust type (for adapter codegen)


@dataclass
class FuncDef:
    name: str
    params: list[Param]
    return_type: str  # Python type string
    kind: str = "method"  # method | new | getter | staticmethod | function
    rust_return_type: str = ""  # Original Rust type (for adapter codegen)


@dataclass
class ClassDef:
    name: str
    methods: list[FuncDef] = field(default_factory=list)
    fields: list[tuple[str, str]] = field(default_factory=list)  # (name, py_type) for get_all
    # Python-facing class name (the ``name = "..."`` override inside a
    # ``#[pyclass(...)]`` attribute). Defaults to the Rust struct name
    # when PyO3 does not rename the class — PyKernel, for example, is
    # exposed to Python as ``Kernel`` via ``#[pyclass(name = "Kernel")]``
    # and must appear as ``class Kernel:`` in the generated stub so
    # ``from nexus_runtime import Kernel`` type-checks.
    py_name: str = ""


@dataclass
class TraitDef:
    name: str
    methods: list[FuncDef] = field(default_factory=list)
    doc: str = ""


# ── Feature-gated exclusions ──────────────────────────────────────
# Functions behind #[cfg(feature = "...")] are not always available.
# Exclude from kernel_exports.py static imports (mypy can't see cfg).
FEATURE_GATED_EXPORTS: set[str] = {
    "openai_chat_completion",
    "openai_chat_completion_stream",
}

# ── Capability group config (single source of truth for _rust_compat.py) ─────
# Defines which module-level symbols belong to each capability group.
# Imported by _rust_compat.py via the generated _kernel_api_groups.py.
# Groups: "core" failure disables all Rust; others disable only their feature.
CAPABILITY_GROUP_CONFIG: dict[str, tuple[str, ...]] = {
    "core": (
        "PyKernel",
        "normalize_path",
        "validate_path",
        "canonicalize_path",
        "extract_zone_id",
        "get_ancestors",
        "get_parent",
        "get_parent_chain",
        "parent_path",
        "path_matches_pattern",
        "split_path",
        "unscope_internal_path",
    ),
    "hash": ("hash_content_py", "hash_content_smart_py"),
    "io": ("read_file", "read_files_bulk"),
    "search": ("grep_bulk", "grep_files_mmap", "glob_match_bulk"),
    "trigram": (
        "build_trigram_index",
        "build_trigram_index_from_entries",
        "invalidate_trigram_cache",
        "trigram_grep",
        "trigram_index_stats",
        "trigram_search_candidates",
    ),
    "rebac": (
        "compute_permission_single",
        "compute_permissions_bulk",
        "expand_subjects",
        "list_objects_for_subject",
    ),
    "storage": (
        "BloomFilter",
        "BlobPackEngine",
    ),
    # Issue #3951: prefix matching helpers used in descendant-access hot paths
    # (rebac visibility, descendant_access, enforcer batch). Gating them here
    # ensures stale/version-skew binaries fall back to the Python implementation
    # in _prefix_helpers.py instead of silently returning wrong auth results.
    "prefix": ("any_path_starts_with", "batch_prefix_check", "filter_paths_by_prefix"),
    "tiger": (
        "any_path_accessible_tiger_cache",
        "check_permission_bitmap",
        "check_permission_bitmap_batch",
        "filter_paths_with_tiger_cache",
        "filter_paths_with_tiger_cache_parallel",
        "intersect_paths_with_tiger_cache",
        "tiger_cache_bitmap_stats",
    ),
}

# ── Return-type overrides ──────────────────────────────────────────
# Rust types like Bound<PyList> don't carry element-type info.
# These overrides supply the correct Python type.

RETURN_OVERRIDES: dict[str, str] = {
    # Functions returning PyList with known element types
    "grep_bulk": "list[dict[str, Any]]",
    "grep_files_mmap": "list[dict[str, Any]]",
    "glob_match_bulk": "list[str]",
    "expand_subjects": "list[tuple[str, str]]",
    "list_objects_for_subject": "list[tuple[str, str]]",
    "trigram_grep": "list[dict[str, Any]]",
    "trigram_search_candidates": "list[str]",
    # Functions returning PyDict with known value types
    "compute_permissions_bulk": "dict[Any, bool]",
    "trigram_index_stats": "dict[str, Any]",
    "tiger_cache_bitmap_stats": "dict[str, Any]",
    "read_files_bulk": "dict[str, bytes]",
    # Class methods returning Py<PyAny> that are actually dicts
    "L1MetadataCache.stats": "dict[str, Any]",
    "L1MetadataCache.get_content": "tuple[bytes, str, bool] | None",
    "VFSSemaphore.info": "dict[str, Any] | None",
    "VFSSemaphore.stats": "dict[str, Any]",
    "RingBufferCore.stats": "dict[str, Any]",
    "RingBufferCore.pop": "bytes",
    "RingBufferCore.peek": "bytes | None",
    "RingBufferCore.peek_all": "list[bytes]",
    "StreamBufferCore.stats": "dict[str, Any]",
    "StreamBufferCore.read_at": "bytes",
    "StreamBufferCore.read_batch": "tuple[list[bytes], int]",
    "WalStreamBackend.read_at": "tuple[bytes, int]",
    "WalStreamBackend.read_batch": "tuple[list[bytes], int]",
    "WalStreamBackend.stats": "dict[str, Any]",
    "Kernel.sys_read": "SysReadResult",
    "Kernel.sys_write": "SysWriteResult",
    "Kernel.sys_stat": "dict[str, Any] | None",
    "Kernel.sys_write_batch": "list[SysWriteResult]",
    "Kernel.sys_read_batch": "list[SysReadResult]",
    "Kernel.sys_unlink_batch": "list[SysUnlinkResult]",
    "VolumeEngine.stats": "dict[str, int]",
    "VolumeEngine.batch_get": "dict[str, bytes]",
}

# ── Param-type overrides (for opaque PyO3 wrappers) ────────────────

PARAM_TYPE_OVERRIDES: dict[str, dict[str, str]] = {
    "compute_permissions_bulk": {
        "checks": "list[Any]",
        "tuples": "list[Any]",
        "namespace_configs": "dict[str, Any]",
    },
    "compute_permission_single": {
        "tuples": "list[Any]",
        "namespace_configs": "dict[str, Any]",
    },
    "expand_subjects": {
        "tuples": "list[Any]",
        "namespace_configs": "dict[str, Any]",
    },
    "list_objects_for_subject": {
        "tuples": "list[Any]",
        "namespace_configs": "dict[str, Any]",
    },
    "grep_bulk": {"file_contents": "dict[str, Any]"},
}

# ── Rust → Python type mapping ─────────────────────────────────────

_SIMPLE_MAP: dict[str, str] = {
    "&str": "str",
    "String": "str",
    "&[u8]": "bytes",
    "bool": "bool",
    "u8": "int",
    "u16": "int",
    "u32": "int",
    "u64": "int",
    "usize": "int",
    "i8": "int",
    "i16": "int",
    "i32": "int",
    "i64": "int",
    "isize": "int",
    "f32": "float",
    "f64": "float",
    "()": "None",
}


def _strip_lifetimes(t: str) -> str:
    """Remove Rust lifetime annotations."""
    t = re.sub(r"<'[a-z_]+,?\s*", "<", t)
    t = re.sub(r",?\s*'[a-z_]+", "", t)
    return t.replace("<>", "").replace("< ", "<").replace(" >", ">").strip()


def _split_top_level(text: str, delim: str = ",") -> list[str]:
    """Split by delimiter respecting nested <>, (), []."""
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in text:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == delim and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def rust_to_py(rust_type: str) -> str:
    """Convert a Rust type string to a Python type annotation."""
    t = _strip_lifetimes(rust_type.strip())

    if t in _SIMPLE_MAP:
        return _SIMPLE_MAP[t]

    # Vec<u8> → bytes (special case before generic Vec)
    if t == "Vec<u8>":
        return "bytes"

    # PyO3 opaque types
    if t in ("Py<PyAny>", "&Bound<PyAny>", "Bound<PyAny>"):
        return "Any"
    if t in ("Py<PyBytes>", "Bound<PyBytes>", "&Bound<PyBytes>"):
        return "bytes"
    if "PyDict" in t:
        return "dict[str, Any]"
    if "PyList" in t:
        return "list[Any]"

    # PyResult<T> → unwrap
    m = re.match(r"PyResult<(.+)>$", t)
    if m:
        return rust_to_py(m.group(1))

    # Option<T> → T | None
    m = re.match(r"Option<(.+)>$", t)
    if m:
        inner = rust_to_py(m.group(1))
        return f"{inner} | None"

    # Vec<T> → list[T]
    m = re.match(r"Vec<(.+)>$", t)
    if m:
        inner = rust_to_py(m.group(1))
        return f"list[{inner}]"

    # HashMap<K, V>
    m = re.match(r"HashMap<(.+)>$", t)
    if m:
        parts = _split_top_level(m.group(1))
        if len(parts) == 2:
            k = rust_to_py(parts[0].strip())
            v = rust_to_py(parts[1].strip())
            return f"dict[{k}, {v}]"

    # Tuple: (A, B, ...)
    if t.startswith("(") and t.endswith(")"):
        inner = t[1:-1]
        parts = _split_top_level(inner)
        converted = [rust_to_py(p.strip()) for p in parts]
        return f"tuple[{', '.join(converted)}]"

    # Self (in static methods)
    if t == "Self":
        return "Self"

    # crate::module::Type → Type (assume same-module PyO3 class)
    m = re.match(r"(?:crate::)?(?:\w+::)*(\w+)$", t)
    if m:
        name = m.group(1)
        # Known PyO3 classes
        if name in (
            "SysReadResult",
            "SysWriteResult",
            "BloomFilter",
        ):
            return name

    return "Any"


# ── Rust source parsing ────────────────────────────────────────────


def _find_matching(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Find matching close bracket from start position (which should be the open bracket)."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _extract_fn_signature(text: str, fn_pos: int) -> tuple[str, list[tuple[str, str]], str | None]:
    """Extract (name, [(param_name, rust_type)], return_type) from a fn definition."""
    # Find function name (may be preceded by pub)
    m = re.match(r"(?:pub(?:\(crate\))?\s+)?fn\s+(\w+)", text[fn_pos:])
    if not m:
        return ("", [], None)
    name = m.group(1)

    # Find opening paren
    paren_start = text.index("(", fn_pos + m.end() - 1)
    paren_end = _find_matching(text, paren_start, "(", ")")
    if paren_end < 0:
        return (name, [], None)

    # Extract params
    params_text = text[paren_start + 1 : paren_end]
    params: list[tuple[str, str]] = []
    for part in _split_top_level(params_text):
        part = part.strip()
        if not part or part in ("&self", "&mut self"):
            continue
        if ":" not in part:
            continue
        pname, ptype = part.split(":", 1)
        pname = pname.strip()
        if pname.startswith("mut "):
            pname = pname[4:]
        ptype = ptype.strip()
        # Skip Python interpreter params
        if pname in ("py", "_py") and "Python" in ptype:
            continue
        params.append((pname, ptype))

    # Find return type: -> TYPE { or -> TYPE where
    rest = text[paren_end + 1 :].lstrip()
    ret_type = None
    if rest.startswith("->"):
        rest = rest[2:].lstrip()
        # Find the end: { or where or ;
        end = len(rest)
        for marker in [" {", "\n{", " where", "\nwhere", ";"]:
            idx = rest.find(marker)
            if 0 <= idx < end:
                end = idx
        ret_type = rest[:end].strip()

    return (name, params, ret_type)


def _find_pyo3_sig_above(text: str, pos: int) -> dict[str, str]:
    """Look backward from pos for #[pyo3(signature = (...))] and extract defaults.

    When the search region contains multiple ``#[pyo3(signature=...)]``
    attributes (e.g. two adjacent methods both annotated), the
    *last* match wins — that's the one immediately above the current
    `fn`.  Earlier code naively grabbed the first match and silently
    inherited the prior method's defaults.
    """
    # Search backward up to 500 chars
    search_start = max(0, pos - 500)
    region = text[search_start:pos]
    matches = list(re.finditer(r"#\[pyo3\(signature\s*=\s*\(([^)]*)\)\)\]", region))
    if not matches:
        return {}
    sig_text = matches[-1].group(1)
    defaults: dict[str, str] = {}
    for part in sig_text.split(","):
        part = part.strip()
        if "=" in part:
            pname, pdefault = part.split("=", 1)
            pname = pname.strip()
            pdefault = pdefault.strip()
            # Convert Rust literals to Python
            if pdefault == "false":
                pdefault = "False"
            elif pdefault == "true":
                pdefault = "True"
            elif pdefault == "None":
                pdefault = "None"
            elif pdefault in ("vec![]", "Vec::new()"):
                pdefault = "[]"
            defaults[pname] = pdefault
    return defaults


def _find_decorators_above(text: str, pos: int) -> list[str]:
    """Look for Rust attributes immediately preceding a fn definition.

    Handles multi-line attributes such as::

        #[new]
        #[pyo3(signature = (
            a=None,
            b=None,
        ))]
        pub fn py_new(...)

    The naive per-line walk would stop at the ``))]`` / ``a=None,`` lines
    that aren't themselves ``#[...]`` starters, missing the earlier
    ``#[new]``. We instead scan the whole preceding region for bare
    ``#[new]`` / ``#[getter]`` / ``#[staticmethod]`` tokens, which is
    both simpler and robust to attribute formatting changes.
    """
    region = text[max(0, pos - 500) : pos]

    # Trim anything before the previous fn / end-of-block so we don't
    # pick up a decorator from a different method.
    for stop_marker in ("\n    }", "\n}", "pub fn ", " fn "):
        idx = region.rfind(stop_marker)
        if idx != -1:
            region = region[idx + len(stop_marker) :]

    decorators: list[str] = []
    if "#[new]" in region:
        decorators.append("new")
    if "#[getter]" in region:
        decorators.append("getter")
    if "#[staticmethod]" in region:
        decorators.append("staticmethod")
    return decorators


def parse_pyfunctions(text: str) -> list[FuncDef]:
    """Extract all #[pyfunction] definitions from file text."""
    results = []
    for m in re.finditer(r"#\[pyfunction\]", text):
        # Find next fn
        fn_m = re.search(r"(?:pub\s+)?fn\s+", text[m.end() :])
        if not fn_m:
            continue
        fn_pos = m.end() + fn_m.start()
        name, params, ret_type = _extract_fn_signature(text, fn_pos)
        if not name:
            continue
        pyo3_defaults = _find_pyo3_sig_above(text, fn_pos)

        # Build Param list
        func_key = name
        py_params = []
        for pname, ptype in params:
            # Check param type override
            if func_key in PARAM_TYPE_OVERRIDES and pname in PARAM_TYPE_OVERRIDES[func_key]:
                pt = PARAM_TYPE_OVERRIDES[func_key][pname]
            else:
                pt = rust_to_py(ptype)
            default = pyo3_defaults.get(pname)
            py_params.append(Param(name=pname, py_type=pt, default=default))

        # Return type
        if func_key in RETURN_OVERRIDES:
            py_ret = RETURN_OVERRIDES[func_key]
        elif ret_type:
            py_ret = rust_to_py(ret_type)
        else:
            py_ret = "None"

        results.append(FuncDef(name=name, params=py_params, return_type=py_ret, kind="function"))
    return results


def parse_pymethods(text: str, class_name: str) -> list[FuncDef]:
    """Extract methods from #[pymethods] impl ClassName { ... } blocks."""
    results = []

    # Find #[pymethods] impl ClassName
    pattern = re.compile(r"#\[pymethods\]\s*impl\s+" + re.escape(class_name) + r"\s*\{")
    for block_m in pattern.finditer(text):
        block_start = block_m.end() - 1  # position of {
        block_end = _find_matching(text, block_start, "{", "}")
        if block_end < 0:
            continue
        block_text = text[block_start : block_end + 1]

        # Find all fn definitions within this block
        for fn_m in re.finditer(r"(?:pub\s+)?fn\s+", block_text):
            fn_pos = block_start + fn_m.start()
            fn_text_pos = fn_m.start()

            name, params, ret_type = _extract_fn_signature(text, fn_pos)
            if not name:
                continue

            # Detect decorators
            decorators = _find_decorators_above(block_text, fn_text_pos)
            pyo3_defaults = _find_pyo3_sig_above(text, fn_pos)

            # Determine kind
            if "new" in decorators:
                kind = "new"
            elif "getter" in decorators:
                kind = "getter"
            elif "staticmethod" in decorators:
                kind = "staticmethod"
            else:
                kind = "method"

            # Build params
            override_key = f"{class_name}.{name}"
            py_params = []
            for pname, ptype in params:
                if (
                    override_key in PARAM_TYPE_OVERRIDES
                    and pname in PARAM_TYPE_OVERRIDES[override_key]
                ):
                    pt = PARAM_TYPE_OVERRIDES[override_key][pname]
                else:
                    pt = rust_to_py(ptype)
                default = pyo3_defaults.get(pname)
                py_params.append(Param(name=pname, py_type=pt, default=default))

            # Return type
            if override_key in RETURN_OVERRIDES:
                py_ret = RETURN_OVERRIDES[override_key]
            elif ret_type:
                py_ret = rust_to_py(ret_type)
            elif kind == "new":
                py_ret = "None"
            else:
                py_ret = "None"

            results.append(FuncDef(name=name, params=py_params, return_type=py_ret, kind=kind))

    return results


def parse_pyclass_name(text: str, class_name: str) -> str:
    """Return the Python-facing name for a ``#[pyclass(...)]`` struct.

    Looks for ``#[pyclass(... name = "X" ...)]`` attached to the struct
    ``class_name`` and returns ``X``; falls back to ``class_name`` if no
    rename is present. Handles both ``#[pyclass(name = "X")]`` and
    ``#[pyclass(name = "X", get_all)]``-style attribute layouts and
    attributes split across lines (``#[pyclass(\n    name = "X",\n)]``).
    """
    pattern = r"#\[pyclass\(([^\)]*)\)\]\s*(?:#\[[^\]]*\]\s*)*(?:pub\s+)?struct\s+" + re.escape(
        class_name
    )
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        return class_name
    attrs = m.group(1)
    name_m = re.search(r'name\s*=\s*"([^"]+)"', attrs)
    return name_m.group(1) if name_m else class_name


def parse_pyclass_fields(text: str, class_name: str) -> list[tuple[str, str]]:
    """Extract #[pyo3(get)] or #[pyclass(get_all)] fields."""
    fields: list[tuple[str, str]] = []
    # Check for get_all
    get_all = bool(
        re.search(r"#\[pyclass\(get_all\)\]\s*(?:pub\s+)?struct\s+" + re.escape(class_name), text)
    )

    # Find struct definition
    m = re.search(r"(?:pub\s+)?struct\s+" + re.escape(class_name) + r"\s*\{", text)
    if not m:
        return fields
    brace_start = m.end() - 1
    brace_end = _find_matching(text, brace_start, "{", "}")
    if brace_end < 0:
        return fields
    body = text[brace_start + 1 : brace_end]
    body_lines = body.split("\n")

    prev_has_get = False
    for line in body_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if stripped.startswith("#[pyo3(get)]"):
            prev_has_get = True
            continue
        if stripped.startswith("#["):
            continue  # other attributes

        has_get = get_all or prev_has_get
        prev_has_get = False
        if not has_get:
            continue
        # Extract field: pub field_name: Type,
        fm = re.search(r"pub\s+(\w+)\s*:\s*(.+?)(?:,|$)", stripped)
        if fm:
            fields.append((fm.group(1), rust_to_py(fm.group(2).strip().rstrip(","))))

    return fields


def parse_traits(text: str) -> list[TraitDef]:
    """Extract trait definitions with their method signatures."""
    results = []
    # Match both `pub(crate) trait Foo` and `pub trait Foo` — kernel
    # traits that external crates (e.g. rust/raft::ZoneMetaStore) impl
    # are `pub`; internal-only traits stay `pub(crate)`.
    for m in re.finditer(r"pub(?:\(crate\))?\s+trait\s+(\w+)\s*(?::\s*[^{]*)?\s*\{", text):
        trait_name = m.group(1)
        brace_start = m.end() - 1
        brace_end = _find_matching(text, brace_start, "{", "}")
        if brace_end < 0:
            continue
        body = text[brace_start + 1 : brace_end]

        methods: list[FuncDef] = []
        for fn_m in re.finditer(r"fn\s+", body):
            fn_pos = brace_start + 1 + fn_m.start()
            name, params, ret_type = _extract_fn_signature(text, fn_pos)
            if not name:
                continue
            py_params = [Param(name=pn, py_type=rust_to_py(pt), rust_type=pt) for pn, pt in params]

            py_ret = rust_to_py(ret_type) if ret_type else "None"

            methods.append(
                FuncDef(
                    name=name,
                    params=py_params,
                    return_type=py_ret,
                    rust_return_type=ret_type or "",
                )
            )

        results.append(TraitDef(name=trait_name, methods=methods))
    return results


# ── lib.rs export parser ──────────────────────────────────────────


def parse_lib_exports(
    text: str,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Parse lib.rs → ([(module, func_name), ...], [(module, class_name), ...]).

    The scanner ignores commented-out call sites — Phase C added doc
    comments that quote ``m.add_class::<MOD::Name>`` to point readers at
    this regex; the regex would otherwise match those quotes and emit
    spurious entries.
    """
    # Strip line comments before scanning so we don't pick up the
    # rustdoc lines that quote `add_class::<…>` / `wrap_pyfunction!(…)`
    # in module-level documentation. Block comments left as-is — they
    # are rare in this file and quoting these macros inside one would
    # be unusual.
    stripped = re.sub(r"//[^\n]*", "", text)

    functions: list[tuple[str, str]] = []
    classes: list[tuple[str, str]] = []

    # Handle multi-line wrap_pyfunction! calls with \s*
    for m in re.finditer(r"wrap_pyfunction!\(\s*(\w+)::(\w+)\s*,", stripped):
        functions.append((m.group(1), m.group(2)))

    for m in re.finditer(r"add_class::<(\w+)::(\w+)>", stripped):
        classes.append((m.group(1), m.group(2)))

    return functions, classes


# ── Stub generator ────────────────────────────────────────────────


def _format_param(p: Param) -> str:
    """Format a parameter for a stub signature.

    `_find_pyo3_sig_above` normalises Rust default literals to Python
    syntax without seeing the parameter type, so `vec![]` always becomes
    `[]`.  PyO3 converts `Vec<u8>` to Python `bytes`, and `bytes = []`
    is a type error — fix it at emit time when both type and default
    are in hand.  Equivalent type-blind conversions (e.g. `Vec<String>`
    → `list[str]` with `[]` default) stay correct, so the rule is
    purely additive.
    """
    default = p.default
    if default == "[]" and p.py_type == "bytes":
        default = 'b""'
    s = f"{p.name}: {p.py_type}"
    if default is not None:
        s += f" = {default}"
    return s


def _format_func_stub(f: FuncDef, indent: str = "") -> str:
    """Format a function/method as a .pyi stub line."""
    params_str = ", ".join(_format_param(p) for p in f.params)

    if f.kind == "new":
        if params_str:
            return f"{indent}def __init__(self, {params_str}) -> None: ..."
        return f"{indent}def __init__(self) -> None: ..."
    elif f.kind == "getter":
        lines = f"{indent}@property\n"
        lines += f"{indent}def {f.name}(self) -> {f.return_type}: ..."
        return lines
    elif f.kind == "staticmethod":
        lines = f"{indent}@staticmethod\n"
        lines += f"{indent}def {f.name}({params_str}) -> {f.return_type}: ..."
        return lines
    elif f.kind == "function":
        return f"{indent}def {f.name}({params_str}) -> {f.return_type}: ..."
    else:
        if params_str:
            return f"{indent}def {f.name}(self, {params_str}) -> {f.return_type}: ..."
        else:
            return f"{indent}def {f.name}(self) -> {f.return_type}: ..."


def generate_stubs(
    module_functions: dict[str, list[FuncDef]],
    classes: dict[str, ClassDef],
    class_order: list[str],
) -> str:
    """Generate the full .pyi stub file."""
    lines = [
        MARKER,
        "# Source: rust/kernel/src/*.rs",
        "",
        '"""Type stubs for nexus_runtime — Rust-accelerated PyO3 extension module.',
        "",
        "Auto-generated from rust/kernel/src/*.rs exports.",
        "Re-run: python scripts/codegen_kernel_abi.py",
        '"""',
        "",
        "from typing import Any, Self  # noqa: F401  (Self used by codegen-emitted classes)",
        "",
    ]

    # Module-level section headers and functions
    SECTION_NAMES: dict[str, str] = {
        "path_utils": "Path utilities (path_utils.rs)",
        "prefix": "Path prefix matching (prefix.rs)",
        "hash": "Hash (hash.rs)",
        "search": "Search (search.rs)",
        "glob": "Glob (glob.rs)",
        "io": "File I/O (io.rs)",
        "rebac": "ReBAC (rebac.rs)",
        "bitmap": "Tiger Cache Bitmap (bitmap.rs)",
        "simd": "SIMD vector similarity (simd.rs)",
        "trigram": "Trigram Index (trigram.rs)",
        "grpc": "VFS gRPC server (rust/transport/src/grpc.rs)",
    }

    MODULE_ORDER = [
        "path_utils",
        "prefix",
        "hash",
        "search",
        "glob",
        "io",
        "rebac",
        "bitmap",
        "simd",
        "trigram",
        "grpc",
        "grpc_bridge",
    ]

    for mod_name in MODULE_ORDER:
        if mod_name not in module_functions:
            continue
        section = SECTION_NAMES.get(mod_name, f"{mod_name}.rs")
        lines.append(f"# {'-' * 75}")
        lines.append(f"# {section}")
        lines.append(f"# {'-' * 75}")
        lines.append("")
        for func in module_functions[mod_name]:
            lines.append(_format_func_stub(func))
        lines.append("")

    # Classes
    lines.append(f"# {'-' * 75}")
    lines.append("# Classes")
    lines.append(f"# {'-' * 75}")
    lines.append("")

    for cls_name in class_order:
        if cls_name not in classes:
            continue
        cls = classes[cls_name]
        # Emit the Python-facing class name (``#[pyclass(name = "X")]``
        # rename). Falls back to the Rust struct name when there is no
        # rename, so existing stubs like ``class BloomFilter:`` are
        # unchanged.
        stub_class_name = cls.py_name or cls.name
        lines.append(f"class {stub_class_name}:")

        # Class-level fields (from get_all or pyo3(get))
        if cls.fields and not cls.methods or cls.fields:
            for fname, ftype in cls.fields:
                lines.append(f"    {fname}: {ftype}")

        # Constructor
        constructors = [m for m in cls.methods if m.kind == "new"]
        if constructors:
            lines.append(_format_func_stub(constructors[0], "    "))

        # Static methods
        statics = [m for m in cls.methods if m.kind == "staticmethod"]
        for sm in statics:
            lines.append(_format_func_stub(sm, "    "))

        # Regular methods
        regulars = [m for m in cls.methods if m.kind == "method"]
        for method in regulars:
            lines.append(_format_func_stub(method, "    "))

        # Getters / properties
        getters = [m for m in cls.methods if m.kind == "getter"]
        for g in getters:
            lines.append(_format_func_stub(g, "    "))

        # If no members at all, add pass
        if not cls.fields and not cls.methods:
            lines.append("    ...")

        lines.append("")

    # ------------------------------------------------------------------
    # Symbols registered by nexus-raft's ``register_python_classes`` —
    # the ``nexus_runtime`` module re-exports them, but the codegen's
    # ``lib.rs`` parser only sees kernel-side ``add_class!`` /
    # ``wrap_pyfunction!`` calls. Until the parser is extended to follow
    # ``register_python_classes``, declare these by hand so mypy can
    # type-check imports like::
    #
    #     from nexus_runtime import TofuTrustStore, hostname_to_node_id
    # ------------------------------------------------------------------
    lines.append("# " + "-" * 75)
    lines.append("# Raft-side classes re-exported via nexus_runtime")
    lines.append("# (rust/raft/src/federation/tofu.rs, rust/raft/src/pyo3_bindings.rs)")
    lines.append("# " + "-" * 75)
    lines.append("")
    lines.append("class PyTrustedZone:")
    lines.append("    zone_id: str")
    lines.append("    ca_fingerprint: str")
    lines.append("    ca_pem: str")
    lines.append("    first_seen: str")
    lines.append("    last_verified: str")
    lines.append("    peer_addresses: list[str]")
    lines.append("")
    lines.append("class PyTofuTrustStore:")
    lines.append("    def __init__(self, path: str) -> None: ...")
    lines.append(
        "    def verify_or_trust(self, zone_id: str, ca_pem: bytes, peer_address: str) -> str: ..."
    )
    lines.append("    def remove(self, zone_id: str) -> bool: ...")
    lines.append("    def get_ca_pem(self, zone_id: str) -> bytes | None: ...")
    lines.append("    def list_trusted(self) -> list[PyTrustedZone]: ...")
    lines.append("    def build_ca_bundle(self, local_ca_path: str) -> str: ...")
    lines.append("    def path(self) -> str: ...")
    lines.append("")
    lines.append("def hostname_to_node_id(hostname: str) -> int: ...")
    lines.append("")
    # Federation control-plane (kernel-internal HAL bridges, exposed to
    # Python as module-level functions — analogue to mkfs / zfs admin).
    lines.append("def install_federation_wiring(kernel: Any) -> None: ...")
    lines.append("def federation_is_initialized(kernel: Any) -> bool: ...")
    lines.append("def federation_create_zone(kernel: Any, zone_id: str) -> str: ...")
    lines.append(
        "def federation_remove_zone(kernel: Any, zone_id: str, force: bool = False) -> None: ..."
    )
    lines.append(
        "def federation_join_zone(kernel: Any, zone_id: str, as_learner: bool = False) -> str: ..."
    )
    lines.append(
        "def federation_share_zone(kernel: Any, local_path: str, new_zone_id: str) -> dict[str, Any]: ..."
    )
    lines.append("def federation_lookup_share(kernel: Any, remote_path: str) -> str | None: ...")
    lines.append("def federation_cluster_info(kernel: Any, zone_id: str) -> dict[str, Any]: ...")
    lines.append("")
    # ── Audit-node stream registration (services::audit::prepare_stream_only)
    #    — hand-written companion to install_audit_hook for nodes that
    #    only collect (not generate) audit traces.
    lines.append("# " + "-" * 75)
    lines.append("# Audit stream-only registration (rust/services/src/python/mod.rs)")
    lines.append("# " + "-" * 75)
    lines.append("")
    lines.append(
        "def prepare_audit_stream_only(kernel: Any, zone_id: str, stream_path: str) -> None: ..."
    )
    lines.append("")
    # ── DeploymentProfile-driven driver gate (services::python) -- hand-
    # written, not codegen.  Python boot calls this with the profile's
    # enabled-driver set before any DT_MOUNT sys_setattr fires.
    lines.append("# " + "-" * 75)
    lines.append("# Driver gate (rust/services/src/python/mod.rs)")
    lines.append("# " + "-" * 75)
    lines.append("")
    lines.append("def nx_set_enabled_drivers(drivers: list[str]) -> None: ...")
    lines.append("")
    # ── ManagedAgentService PyO3 surface (services::python) -- hand-written,
    # not codegen. Boot-installer for AgentKind::MANAGED hooks +
    # session lifecycle.
    lines.append("# " + "-" * 75)
    lines.append("# ManagedAgentService PyO3 surface (rust/services/src/python/mod.rs)")
    lines.append("# " + "-" * 75)
    lines.append("")
    lines.append("def nx_managed_agent_install(py_kernel: Any) -> None: ...")
    lines.append("")
    # ── AcpService PyO3 surface (services::acp::pyo3) — hand-written,
    # NOT codegen. Hosts AgentKind::UNMANAGED agents via subprocess +
    # ACP-over-stdio.
    lines.append("# " + "-" * 75)
    lines.append("# AcpService PyO3 surface (rust/services/src/acp/pyo3.rs)")
    lines.append("# " + "-" * 75)
    lines.append("")
    lines.append('def nx_acp_install(py_kernel: Any, default_zone: str = "root") -> None: ...')
    lines.append("def nx_acp_set_agent_registry(py_kernel: Any, registry: Any) -> None: ...")
    lines.append(
        "def nx_acp_register_on_terminate(py_kernel: Any, callback_id: str, callback: Any) -> None: ..."
    )
    lines.append("")
    # ── Generic Rust-service dispatch entry point (services::python) — same
    # lookup the tonic Call handler uses; in-process callers should
    # prefer this over per-service shortcuts so audit/permission hooks
    # can land in one place.
    lines.append("# " + "-" * 75)
    lines.append("# Generic Rust-service dispatch (rust/services/src/python/mod.rs)")
    lines.append("# " + "-" * 75)
    lines.append("")
    lines.append(
        "def nx_kernel_dispatch_rust_call(py_kernel: Any, service: str, method: str, payload: bytes) -> bytes | None: ..."
    )
    lines.append("")

    # ──────────────────────────────────────────────────────────────────
    # Manual section: PrefetchEngine (Issue #4057).  The class is
    # implemented in rust/nexus-prefetch/src/pyo3_bindings.rs, not in
    # rust/kernel/src, so the auto-scan above doesn't see it.  Keeping
    # the canonical stub in this script keeps `Codegen Sync Check`
    # consistent — re-running codegen reproduces the same output.
    # ──────────────────────────────────────────────────────────────────
    lines.append("# " + "-" * 75)
    lines.append("# PrefetchEngine PyO3 surface (rust/nexus-prefetch/src/pyo3_bindings.rs)")
    lines.append("# Issue #4057 — adaptive prefetcher exposed to Python via cdylib.  Manually")
    lines.append("# maintained: the codegen at scripts/codegen_kernel_abi.py only scans")
    lines.append("# rust/kernel/src; PrefetchEngine lives in rust/nexus-prefetch.")
    lines.append("# " + "-" * 75)
    lines.append("")
    lines.append("class PrefetchEngine:")
    lines.append("    def __init__(")
    lines.append("        self,")
    lines.append("        read_callable: Any,")
    lines.append("        block_size: int,")
    lines.append("        initial_window: int,")
    lines.append("        max_window: int,")
    lines.append("        max_workers: int,")
    lines.append("        queue_capacity: int,")
    lines.append("        max_blocks_per_trigger: int,")
    lines.append("        sequential_tolerance: int,")
    lines.append("        min_sequential_count: int,")
    lines.append('        detector: str = "sequential",')
    lines.append("        shutdown_timeout_ms: int = 2000,")
    lines.append("        max_buffer_bytes: int = 134217728,")
    lines.append("    ) -> None: ...")
    lines.append("    def on_open(self, fh: int, path: str, file_size: int | None) -> None: ...")
    lines.append("    def on_read(self, fh: int, offset: int, size: int) -> bytes | None: ...")
    lines.append("    def on_release(self, fh: int) -> None: ...")
    lines.append("    def invalidate_fh(self, fh: int) -> None: ...")
    lines.append("    def invalidate_path(self, path: str) -> None: ...")
    lines.append("    def metrics(self) -> tuple[int, int, int, int, int]: ...")
    lines.append("    def shutdown(self) -> None: ...")

    return "\n".join(lines)


# ── Protocol generator ────────────────────────────────────────────


def generate_protocols(traits: list[TraitDef]) -> str:
    """Generate Python Protocol classes from Rust trait definitions."""
    lines = [
        MARKER,
        "# Source: rust/kernel/src/dispatch.rs, metastore.rs, backend.rs",
        "",
        '"""Kernel dispatch protocols — Python typing contracts for Rust traits.',
        "",
        "These Protocol classes mirror Rust traits in the kernel. Implement them",
        "in Python to provide hooks, resolvers, observers, or storage backends.",
        "",
        "Re-run: python scripts/codegen_kernel_abi.py",
        '"""',
        "from __future__ import annotations",
        "",
        "from typing import Any, Protocol, runtime_checkable",
        "",
    ]

    for trait in traits:
        lines.append("")
        lines.append("@runtime_checkable")
        lines.append(f"class {trait.name}(Protocol):")
        if trait.doc:
            lines.append(f'    """{trait.doc}"""')
            lines.append("")

        if not trait.methods:
            lines.append("    ...")
            continue

        for method in trait.methods:
            params_str = ", ".join(_format_param(p) for p in method.params)
            if params_str:
                sig = f"    def {method.name}(self, {params_str}) -> {method.return_type}: ..."
            else:
                sig = f"    def {method.name}(self) -> {method.return_type}: ..."
            lines.append(sig)

        lines.append("")

    return "\n".join(lines)


# ── Exports generator ─────────────────────────────────────────────


def generate_exports(all_names: list[str]) -> str:
    """Generate kernel_exports.py — Python re-export module."""

    # Match ruff-isort's default order for `from X import (...)` blocks:
    # 1) PascalCase / SCREAMING_CASE identifiers (first char uppercase)
    #    sorted before lowercase / snake_case identifiers,
    # 2) within each group, case-insensitive alphabetical (with ASCII
    #    tiebreaker for stability).
    # Without exactly mirroring ruff's grouping, the codegen-sync hook
    # ping-pongs against `ruff --fix` on every commit.
    def _isort_key(name: str) -> tuple[int, str, str]:
        first = name[:1]
        case_group = 0 if first.isupper() else 1
        return (case_group, name.lower(), name)

    static_names = [n for n in sorted(all_names, key=_isort_key) if n not in FEATURE_GATED_EXPORTS]

    lines = [
        MARKER,
        "# Source: rust/kernel/src/lib.rs",
        "",
        '"""Kernel re-export module — consolidated kernel boundary.',
        "",
        "Usage::",
        "",
        "    from nexus.core.kernel_exports import Kernel, SysReadResult",
        "",
        "Re-run: python scripts/codegen_kernel_abi.py",
        '"""',
        "from __future__ import annotations",
        "",
        "from nexus_runtime import (",
    ]

    for name in static_names:
        lines.append(f"    {name},")

    lines.append(")")
    lines.append("")

    # Feature-gated functions (behind #[cfg]) are NOT re-exported here.
    # Import directly from nexus_runtime when needed.

    # __all__ for clean star imports (excludes feature-gated names)
    lines.append("__all__ = [")
    for name in static_names:
        lines.append(f'    "{name}",')
    lines.append("]")
    lines.append("")

    return "\n".join(lines)


def generate_api_groups(classes: dict[str, "ClassDef"]) -> str:
    """Generate _kernel_api_groups.py — auto-derived API surface for version validation.

    Emits:
      MODULE_CAPABILITY_GROUPS — module-level symbol groups (moved from _rust_compat.py)
      KERNEL_REQUIRED_METHODS  — all public methods on PyKernel (auto-derived from Rust)
    """
    kernel_cls = classes.get("PyKernel")
    if kernel_cls is None:
        raise ValueError("PyKernel class not found in parsed Rust sources")

    # All public instance/static methods (skip __init__, getters, and private _methods).
    # Getters are excluded by design: a missing getter is degradable at the call
    # site (use `getattr(..., None)` + warn) rather than fatal. Including them
    # here would mark the whole kernel unusable and short-circuit boot before any
    # consumer-level fallback can run (Issue #4017 regression class).
    kernel_methods = sorted(
        f.name
        for f in kernel_cls.methods
        if f.kind not in ("new", "getter") and not f.name.startswith("__")
    )

    lines = [
        MARKER,
        "# Source: rust/kernel/src/kernel.rs (PyKernel methods)",
        "",
        '"""Auto-generated API surface groups for nexus_runtime version validation.',
        "",
        "Used by nexus._rust_compat to validate the installed binary against the",
        "expected API surface at import time.  Re-run: python scripts/codegen_kernel_abi.py",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "# Module-level symbols grouped by capability area.",
        "# Sourced from CAPABILITY_GROUP_CONFIG in scripts/codegen_kernel_abi.py.",
        "MODULE_CAPABILITY_GROUPS: dict[str, tuple[str, ...]] = {",
    ]

    for group, symbols in CAPABILITY_GROUP_CONFIG.items():
        lines.append(f'    "{group}": (')
        for sym in symbols:
            lines.append(f'        "{sym}",')
        lines.append("    ),")

    lines += [
        "}",
        "",
        "# All public methods that must exist on nexus_runtime.PyKernel.",
        "# Auto-derived from #[pymethods] in rust/kernel/src/kernel.rs.",
        "# A stale binary missing any of these triggers an actionable ImportError.",
        "KERNEL_REQUIRED_METHODS: frozenset[str] = frozenset({",
    ]

    for name in kernel_methods:
        lines.append(f'    "{name}",')

    lines += [
        "})",
        "",
    ]

    return "\n".join(lines)


# ── Pillar adapter generator (Direction 3 — Rust output) ─────────

# Configuration for pillar adapters: trait name → adapter generation params.
# These adapters wrap Python ABCs → Rust traits via PyO3 (transitional).

PILLAR_ADAPTERS: dict[str, dict[str, str]] = {
    # PyObjectStoreAdapter removed — all connectors now have Rust-native backends
    # (Crossing 1 elimination, Phase 5D). No Python fallback needed.
    # "ObjectStore": { ... },
    # PyMetaStoreAdapter removed (Phase 9) — Rust kernel uses LocalMetaStore directly.
    # "MetaStore": { ... },
}

# Methods that need special handling beyond simple call_method1 + extract.
# Key: "TraitName.method_name", value: override tag.
ADAPTER_METHOD_OVERRIDES: dict[str, str] = {
    # ObjectStore overrides removed — PyObjectStoreAdapter deleted (Phase 5D).
    # MetaStore.get() → check None + extract_metadata
    "MetaStore.get": "OPTION_FILE_METADATA",
    # MetaStore.put() → to_python_metadata for FileMetadata param
    "MetaStore.put": "PUT_FILE_METADATA",
    # MetaStore.list() → iterate + extract_metadata
    "MetaStore.list": "VEC_FILE_METADATA",
}


def _rust_err_map(config: dict[str, str], method_name: str, first_param: str) -> str:
    """Generate the .map_err(...) expression for a given error type."""
    if config["error"] == "StorageError":
        return "StorageError::IOError(io::Error::other(e.to_string()))"
    else:
        # Use :? for slice params (e.g. paths: &[String]) since &[T] doesn't impl Display
        fmt_spec = ":?" if first_param in ("paths", "items") else ""
        return f'MetaStoreError::IOError(format!("metastore.{method_name}({{{first_param}{fmt_spec}}}): {{e}}"))'


def _generate_adapter_method(
    trait_name: str,
    method: FuncDef,
    config: dict[str, str],
) -> list[str]:
    """Generate a single adapter trait impl method as Rust lines."""
    key = f"{trait_name}.{method.name}"
    override = ADAPTER_METHOD_OVERRIDES.get(key)
    params = method.params
    first_param = params[0].name if params else '""'
    if len(params) == 1:
        tuple_arg = f"({params[0].name},)"
    else:
        tuple_arg = f"({', '.join(p.name for p in params)})"

    # Build method signature (Rust)
    sig_params = ["&self"]
    for p in params:
        sig_params.append(f"{p.name}: {p.rust_type}")
    sig = ", ".join(sig_params)
    ret = method.rust_return_type

    lines: list[str] = []

    if override == "CACHED_NAME":
        # name() → return cached field
        lines.append("    fn name(&self) -> &str {")
        lines.append(f"        &self.{config['cached_name']}")
        lines.append("    }")
        return lines

    # Check if signature fits on one line (rustfmt limit ~100 chars)
    one_line = f"    fn {method.name}({sig}) -> {ret} {{"
    if len(one_line) <= 100:
        lines.append(one_line)
    else:
        lines.append(f"    fn {method.name}(")
        for i, sp in enumerate(sig_params):
            comma = "," if i < len(sig_params) - 1 else ","
            lines.append(f"        {sp}{comma}")
        lines.append(f"    ) -> {ret} {{")

    if override == "READ_WITH_CONTEXT":
        # read_content(content_id, backend_path, ctx): convert Rust OperationContext → Python
        err = _rust_err_map(config, method.name, first_param)
        lines.append("        Python::attach(|py| {")
        lines.append("            let obj = self.inner.bind(py);")
        lines.append("            // Convert Rust OperationContext → Python OperationContext")
        lines.append("            let py_ctx = rust_ctx_to_python(py, ctx, backend_path)")
        lines.append("                .map_err(|e| StorageError::IOError(io::Error::other(e)))?;")
        lines.append("            let result = obj")
        lines.append("                .call_method(")
        lines.append('                    "read_content",')
        lines.append("                    (content_id,),")
        lines.append("                    Some(&{")
        lines.append("                        let kw = pyo3::types::PyDict::new(py);")
        lines.append('                        let _ = kw.set_item("context", &py_ctx);')
        lines.append("                        kw")
        lines.append("                    }),")
        lines.append("                )")
        lines.append(f"                .map_err(|e| {err})?;")
        lines.append("            result")
        lines.append("                .extract::<Vec<u8>>()")
        lines.append(f"                .map_err(|e| {err})")
        lines.append("        })")
        lines.append("    }")
        return lines

    if override == "WRITE_RESULT":
        # write_content(content, content_id, *, offset, context): Python returns WriteResult
        # Pass content_id as positional arg, offset + ctx as keyword args.
        err = _rust_err_map(config, method.name, first_param)
        lines.append("        Python::attach(|py| {")
        lines.append("            let obj = self.inner.bind(py);")
        lines.append(
            "            // Convert Rust ctx → Python OperationContext (carries backend_path for PAS)"
        )
        lines.append("            let py_ctx = rust_ctx_to_python(py, ctx, content_id)")
        lines.append("                .map_err(|e| StorageError::IOError(io::Error::other(e)))?;")
        lines.append("            let kwargs = pyo3::types::PyDict::new(py);")
        lines.append('            let _ = kwargs.set_item("context", &py_ctx);')
        lines.append("            // R20.10: thread offset as a kwarg so Python backends")
        lines.append("            // (PAS-addressing-engine RMW) honor POSIX pwrite semantics.")
        lines.append('            let _ = kwargs.set_item("offset", offset);')
        lines.append("            let result = obj")
        lines.append(
            '                .call_method("write_content", (content, content_id), Some(&kwargs))'
        )
        lines.append(f"                .map_err(|e| {err})?;")
        lines.append("            // Python ObjectStoreABC.write_content() returns WriteResult")
        lines.append("            let cid = result")
        lines.append('                .getattr("content_id")')
        lines.append("                .and_then(|v| v.extract::<String>())")
        lines.append(f"                .map_err(|e| {err})?;")
        lines.append("            let version = result")
        lines.append('                .getattr("version")')
        lines.append("                .and_then(|v| v.extract::<String>())")
        lines.append("                .unwrap_or_else(|_| cid.clone());")
        lines.append("            let size = result")
        lines.append('                .getattr("size")')
        lines.append("                .and_then(|v| v.extract::<u64>())")
        lines.append("                .unwrap_or(content.len() as u64);")
        lines.append("            Ok(WriteResult {")
        lines.append("                content_id: cid,")
        lines.append("                version,")
        lines.append("                size,")
        lines.append("            })")
        lines.append("        })")
        lines.append("    }")
        return lines

    if override == "COPY_FILE":
        # copy_file(src_path, dst_path) → hasattr check + call + get_size/version
        lines.append("        Python::attach(|py| {")
        lines.append("            let obj = self.inner.bind(py);")
        lines.append("            // Check if the Python backend has copy_file (PAS-only method)")
        lines.append('            if !obj.hasattr("copy_file").unwrap_or(false) {')
        lines.append('                return Err(StorageError::NotSupported("copy_file"));')
        lines.append("            }")
        lines.append('            obj.call_method1("copy_file", (src_path, dst_path))')
        lines.append(
            "                .map_err(|e| StorageError::IOError(io::Error::other(e.to_string())))?;"
        )
        lines.append(
            "            // copy_file returns None in Python; compute result from destination"
        )
        lines.append("            let size = obj")
        lines.append('                .call_method1("get_size_by_path", (dst_path,))')
        lines.append("                .and_then(|v| v.extract::<u64>())")
        lines.append("                .unwrap_or(0);")
        lines.append("            let version = obj")
        lines.append('                .call_method1("get_version_by_path", (dst_path,))')
        lines.append("                .and_then(|v| v.extract::<String>())")
        lines.append("                .unwrap_or_default();")
        lines.append("            Ok(WriteResult {")
        lines.append("                content_id: version.clone(),")
        lines.append("                version,")
        lines.append("                size,")
        lines.append("            })")
        lines.append("        })")
        lines.append("    }")
        return lines

    if override == "OPTION_FILE_METADATA":
        err = _rust_err_map(config, method.name, first_param)
        lines.append("        Python::attach(|py| {")
        lines.append("            let obj = self.inner.bind(py);")
        lines.append("            let result = obj")
        lines.append(f'                .call_method1("{method.name}", {tuple_arg})')
        lines.append(f"                .map_err(|e| {err})?;")
        lines.append("            if result.is_none() {")
        lines.append("                return Ok(None);")
        lines.append("            }")
        lines.append("            extract_metadata(py, &result).map(Some)")
        lines.append("        })")
        lines.append("    }")
        return lines

    if override == "PUT_FILE_METADATA":
        err = _rust_err_map(config, method.name, first_param)
        lines.append("        Python::attach(|py| {")
        lines.append("            let obj = self.inner.bind(py);")
        lines.append("            let py_meta = to_python_metadata(py, &metadata)?;")
        lines.append(f'            obj.call_method1("{method.name}", ({first_param}, py_meta))')
        lines.append(f"                .map_err(|e| {err})?;")
        lines.append("            Ok(())")
        lines.append("        })")
        lines.append("    }")
        return lines

    if override == "VEC_FILE_METADATA":
        err = _rust_err_map(config, method.name, first_param)
        lines.append("        Python::attach(|py| {")
        lines.append("            let obj = self.inner.bind(py);")
        lines.append("            let result = obj")
        lines.append(f'                .call_method1("{method.name}", {tuple_arg})')
        lines.append(f"                .map_err(|e| {err})?;")
        lines.append("            let iter = result")
        lines.append("                .try_iter()")
        lines.append(
            f'                .map_err(|e| MetaStoreError::IOError(format!("metastore.{method.name} iter: {{e}}")))?;'
        )
        lines.append("            let mut items = Vec::new();")
        lines.append("            for item in iter {")
        lines.append("                let item =")
        lines.append(
            f'                    item.map_err(|e| MetaStoreError::IOError(format!("metastore.{method.name} item: {{e}}")))?;'
        )
        lines.append("                items.push(extract_metadata(py, &item)?);")
        lines.append("            }")
        lines.append("            Ok(items)")
        lines.append("        })")
        lines.append("    }")
        return lines

    # Default pattern: call_method1 + extract or unit
    err = _rust_err_map(config, method.name, first_param)
    # Determine if return is unit Result<(), E> vs Result<T, E>
    inner_type = _extract_result_inner(ret)

    lines.append("        Python::attach(|py| {")
    lines.append("            let obj = self.inner.bind(py);")

    if inner_type == "()":
        # Unit return: call and discard
        lines.append(f'            obj.call_method1("{method.name}", {tuple_arg})')
        lines.append(f"                .map_err(|e| {err})?;")
        lines.append("            Ok(())")
    else:
        # Extract typed return
        rust_extract = _rust_extract_type(inner_type)
        lines.append("            let result = obj")
        lines.append(f'                .call_method1("{method.name}", {tuple_arg})')
        lines.append(f"                .map_err(|e| {err})?;")
        lines.append("            result")
        lines.append(f"                .extract::<{rust_extract}>()")
        lines.append(f"                .map_err(|e| {err})")

    lines.append("        })")
    lines.append("    }")
    return lines


def _extract_result_inner(rust_type: str) -> str:
    """Extract T from Result<T, E>."""
    m = re.match(r"Result<(.+),\s*\w+>$", rust_type)
    if m:
        return m.group(1).strip()
    return rust_type


def _rust_extract_type(inner: str) -> str:
    """Map Rust inner type to PyO3 extract type."""
    mapping = {
        "String": "String",
        "bool": "bool",
        "u64": "u64",
        "u32": "u32",
        "Vec<u8>": "Vec<u8>",
    }
    return mapping.get(inner, inner)


def generate_pillar_adapters(traits: list[TraitDef]) -> str:
    """Generate Rust adapter implementations from parsed traits.

    Direction 3 (PILLAR): Wraps Python ABCs → Rust traits via PyO3.
    Output: rust/kernel/src/generated_adapters.rs
    """
    trait_map = {t.name: t for t in traits}

    lines = [
        RUST_MARKER,
        "//! Pillar adapter implementations — wraps Python ABCs → Rust traits via PyO3.",
        "//!",
        "//! Direction 3 (PILLAR): Python storage backends → Rust trait implementations.",
        "//! These adapters are transitional — replaced by Rust native or gRPC.",
        "//!",
        "//! Re-generate: python scripts/codegen_kernel_abi.py",
        "",
        "#![allow(dead_code)]",
        "",
        "use pyo3::prelude::*;",
        "use pyo3::types::PyDict;",
        "use std::io;",
        "",
        "use crate::abc::object_store::{ObjectStore, StorageError};",
        "use crate::meta_store::{FileMetadata, MetaStoreError};",
        "",
        "// ── FileMetadata conversion helpers (PyO3-specific) ──────────────────────",
        "",
        "/// Extract Rust FileMetadata from a Python FileMetadata object.",
        "fn extract_metadata(",
        "    py: Python<'_>,",
        "    obj: &Bound<'_, PyAny>,",
        ") -> Result<FileMetadata, MetaStoreError> {",
        "    let get_str = |name: &str| -> Result<String, MetaStoreError> {",
        "        obj.getattr(name)",
        "            .and_then(|v| v.extract::<String>())",
        '            .map_err(|e| MetaStoreError::IOError(format!("field {name}: {e}")))',
        "    };",
        "    let get_opt_str = |name: &str| -> Result<Option<String>, MetaStoreError> {",
        "        match obj.getattr(name) {",
        "            Ok(v) if v.is_none() => Ok(None),",
        "            Ok(v) => v",
        "                .extract::<String>()",
        "                .map(Some)",
        '                .map_err(|e| MetaStoreError::IOError(format!("field {name}: {e}"))),',
        '            Err(e) => Err(MetaStoreError::IOError(format!("field {name}: {e}"))),',
        "        }",
        "    };",
        "    let get_u64_or_zero = |name: &str| -> Result<u64, MetaStoreError> {",
        "        match obj.getattr(name) {",
        "            Ok(v) => v",
        "                .extract::<u64>()",
        '                .map_err(|e| MetaStoreError::IOError(format!("field {name}: {e}"))),',
        "            Err(_) => Ok(0),",
        "        }",
        "    };",
        "",
        "    let _ = py;",
        "    Ok(FileMetadata {",
        '        path: get_str("path")?,',
        "        size: obj",
        '            .getattr("size")',
        "            .and_then(|v| v.extract::<u64>())",
        '            .map_err(|e| MetaStoreError::IOError(format!("field size: {e}")))?,',
        '        content_id: get_opt_str("content_id")?,',
        '        gen: get_u64_or_zero("gen")?,',
        "        version: obj",
        '            .getattr("version")',
        "            .and_then(|v| v.extract::<u32>())",
        '            .map_err(|e| MetaStoreError::IOError(format!("field version: {e}")))?,',
        "        entry_type: obj",
        '            .getattr("entry_type")',
        "            .and_then(|v| v.extract::<u8>())",
        '            .map_err(|e| MetaStoreError::IOError(format!("field entry_type: {e}")))?,',
        '        zone_id: get_opt_str("zone_id")?,',
        '        mime_type: get_opt_str("mime_type")?,',
        '        created_at_ms: extract_opt_datetime_ms(obj, "created_at"),',
        '        modified_at_ms: extract_opt_datetime_ms(obj, "modified_at"),',
        '        last_writer_address: get_opt_str("last_writer_address")?,',
        '        target_zone_id: get_opt_str("target_zone_id")?,',
        '        link_target: get_opt_str("link_target").ok().flatten(),',
        "    })",
        "}",
        "",
        "/// Convert a Python ``datetime`` attribute to epoch milliseconds.",
        "/// Returns None if the attribute is missing or not a datetime.",
        "fn extract_opt_datetime_ms(obj: &Bound<'_, PyAny>, name: &str) -> Option<i64> {",
        "    let v = obj.getattr(name).ok()?;",
        "    if v.is_none() {",
        "        return None;",
        "    }",
        '    let ts = v.call_method0("timestamp").ok()?;',
        "    let secs = ts.extract::<f64>().ok()?;",
        "    Some((secs * 1000.0) as i64)",
        "}",
        "",
        "/// Convert Rust FileMetadata → Python FileMetadata (for metastore.put()).",
        "fn to_python_metadata<'py>(",
        "    py: Python<'py>,",
        "    meta: &FileMetadata,",
        ") -> Result<Bound<'py, PyAny>, MetaStoreError> {",
        "    fn err(e: PyErr) -> MetaStoreError {",
        '        MetaStoreError::IOError(format!("to_python_metadata: {e}"))',
        "    }",
        "    let cls = py",
        '        .import("nexus.contracts.metadata")',
        "        .map_err(err)?",
        '        .getattr("FileMetadata")',
        "        .map_err(err)?;",
        "    let kwargs = PyDict::new(py);",
        '    kwargs.set_item("path", &meta.path).map_err(err)?;',
        '    kwargs.set_item("size", meta.size).map_err(err)?;',
        '    kwargs.set_item("content_id", meta.content_id.as_deref()).map_err(err)?;',
        '    kwargs.set_item("gen", meta.gen).map_err(err)?;',
        '    kwargs.set_item("version", meta.version).map_err(err)?;',
        "    kwargs",
        '        .set_item("entry_type", meta.entry_type)',
        "        .map_err(err)?;",
        "    kwargs",
        '        .set_item("zone_id", meta.zone_id.as_deref())',
        "        .map_err(err)?;",
        "    kwargs",
        '        .set_item("mime_type", meta.mime_type.as_deref())',
        "        .map_err(err)?;",
        '    set_optional_datetime(py, &kwargs, "created_at", meta.created_at_ms).map_err(err)?;',
        '    set_optional_datetime(py, &kwargs, "modified_at", meta.modified_at_ms).map_err(err)?;',
        "    if let Some(target) = meta.link_target.as_deref() {",
        '        kwargs.set_item("link_target", target).map_err(err)?;',
        "    }",
        "    cls.call((), Some(&kwargs)).map_err(err)",
        "}",
        "",
        "/// Set a kwargs entry to a UTC datetime built from epoch ms (or None).",
        "fn set_optional_datetime(",
        "    py: Python<'_>,",
        "    kwargs: &Bound<'_, PyDict>,",
        "    key: &str,",
        "    ms: Option<i64>,",
        ") -> PyResult<()> {",
        "    let Some(ms) = ms else {",
        "        return kwargs.set_item(key, py.None());",
        "    };",
        '    let datetime = py.import("datetime")?;',
        '    let utc = datetime.getattr("timezone")?.getattr("utc")?;',
        '    let from_ts = datetime.getattr("datetime")?.getattr("fromtimestamp")?;',
        "    let secs = ms as f64 / 1000.0;",
        "    let dt = from_ts.call((secs, &utc), None)?;",
        "    kwargs.set_item(key, dt)",
        "}",
        "",
        "/// Set a stat-dict entry to a UTC ISO-8601 string built from epoch ms.",
        "/// Pure Rust (chrono) — no Python crossing.",
        "fn set_optional_iso_datetime(",
        "    py: Python<'_>,",
        "    dict: &Bound<'_, PyDict>,",
        "    key: &str,",
        "    ms: Option<i64>,",
        ") -> PyResult<()> {",
        "    let Some(ms) = ms else {",
        "        return dict.set_item(key, py.None());",
        "    };",
        "    let secs = ms / 1000;",
        "    let nsecs = ((ms % 1000) * 1_000_000) as u32;",
        "    if let Some(dt) = chrono::DateTime::from_timestamp(secs, nsecs) {",
        "        dict.set_item(key, dt.to_rfc3339_opts(chrono::SecondsFormat::Millis, true))",
        "    } else {",
        "        dict.set_item(key, py.None())",
        "    }",
        "}",
        "",
        "/// Convert Rust OperationContext → Python OperationContext.",
        "///",
        "/// Bridges Rust kernel credential to Python backend's expected context type.",
        "fn rust_ctx_to_python<'py>(",
        "    py: Python<'py>,",
        "    ctx: &crate::kernel::OperationContext,",
        "    backend_path: &str,",
        ") -> Result<Bound<'py, PyAny>, String> {",
        "    let cls = py",
        '        .import("nexus.contracts.types")',
        '        .and_then(|m| m.getattr("OperationContext"))',
        '        .map_err(|e| format!("import OperationContext: {e}"))?;',
        "    let kwargs = PyDict::new(py);",
        '    let _ = kwargs.set_item("user_id", &ctx.user_id);',
        '    let _ = kwargs.set_item("zone_id", &ctx.zone_id);',
        '    let _ = kwargs.set_item("is_admin", ctx.is_admin);',
        '    let _ = kwargs.set_item("is_system", ctx.is_system);',
        '    let _ = kwargs.set_item("backend_path", backend_path);',
        '    let _ = kwargs.set_item("groups", pyo3::types::PyList::empty(py));',
        "    if let Some(ref agent_id) = ctx.agent_id {",
        '        let _ = kwargs.set_item("agent_id", agent_id);',
        "    }",
        "    cls.call((), Some(&kwargs))",
        '        .map_err(|e| format!("OperationContext(): {e}"))',
        "}",
    ]

    # Generate each adapter
    for trait_name, config in PILLAR_ADAPTERS.items():
        trait = trait_map.get(trait_name)
        if trait is None:
            continue

        adapter_name = config["adapter"]
        cached_name = config.get("cached_name", "")

        lines.append("")
        lines.append(f"// ── {adapter_name} " + "─" * (60 - len(adapter_name)))
        lines.append("")

        # Doc comment
        lines.append(f"/// Wraps Python `{trait_name}ABC` → Rust `{trait_name}` trait.")
        lines.append("///")
        lines.append("/// Transitional adapter: Python backend via GIL (cold path).")

        # Struct definition
        lines.append(f"pub(crate) struct {adapter_name} {{")
        lines.append("    inner: Py<PyAny>,")
        if cached_name:
            lines.append(f"    {cached_name}: String,")
        lines.append("}")
        lines.append("")
        lines.append(f"unsafe impl Send for {adapter_name} {{}}")
        lines.append(f"unsafe impl Sync for {adapter_name} {{}}")

        # Constructor
        lines.append("")
        lines.append(f"impl {adapter_name} {{")
        if cached_name:
            lines.append("    pub(crate) fn new(py: Python<'_>, inner: Py<PyAny>) -> Self {")
            lines.append("        let name = inner")
            lines.append("            .bind(py)")
            lines.append('            .getattr("name")')
            lines.append("            .and_then(|n| n.extract::<String>())")
            lines.append('            .unwrap_or_else(|_| "<backend>".to_string());')
            lines.append("        Self {")
            lines.append("            inner,")
            lines.append(f"            {cached_name}: name,")
            lines.append("        }")
            lines.append("    }")
        else:
            lines.append("    pub(crate) fn new(inner: Py<PyAny>) -> Self {")
            lines.append("        Self { inner }")
            lines.append("    }")
        lines.append("}")

        # Trait impl
        lines.append("")
        skip_methods = config.get("skip_methods", set())
        lines.append(f"impl {trait_name} for {adapter_name} {{")
        for method in trait.methods:
            if method.name in skip_methods:
                continue
            method_lines = _generate_adapter_method(trait_name, method, config)
            lines.extend(method_lines)
            lines.append("")
        # Remove trailing blank line inside impl block
        if lines and lines[-1] == "":
            lines.pop()
        lines.append("}")

    lines.append("")
    return "\n".join(lines)


# ── Dispatch adapter generator (Direction 2 — Rust output) ───────

# Dispatch adapter configs: trait → adapter struct name + cached_name
DISPATCH_ADAPTERS: dict[str, dict[str, str]] = {
    "InterceptHook": {"adapter": "PyInterceptHookAdapter", "cached_name": "hook_name"},
    "PathResolver": {"adapter": "PyPathResolverAdapter", "cached_name": ""},
}


def _gen_dispatch_method(trait_name: str, method: FuncDef) -> list[str]:
    """Generate a single dispatch adapter method body."""
    name = method.name
    ret = method.rust_return_type
    params = method.params
    lines: list[str] = []

    # name() → cached field
    if name == "name" and "&str" in ret:
        config = DISPATCH_ADAPTERS.get(trait_name, {})
        cn = config.get("cached_name", "")
        if cn:
            lines.append("    fn name(&self) -> &str {")
            lines.append(f"        &self.{cn}")
            lines.append("    }")
            return lines

    # Build Rust signature
    sig_params = ["&self"]
    for p in params:
        sig_params.append(f"{p.name}: {p.rust_type}")
    sig = ", ".join(sig_params)

    # ── Special case: &FileEvent parameter (MutationObserver::on_mutation)
    #
    # The trait passes a `&FileEvent` (Rust struct from dispatch.rs). PyO3
    # cannot auto-convert it, so we route through the `file_event_to_py`
    # helper emitted at the top of `_dispatch_adapter_bodies`. This is the
    # only place a Rust struct crosses into Python in the dispatch direction;
    # if we add more such structs we should generalize this.
    file_event_param = next(
        (p for p in params if p.rust_type == "&FileEvent"),
        None,
    )
    if file_event_param is not None:
        lines.append(f"    fn {name}({sig}) {{")
        lines.append("        Python::attach(|py| {")
        lines.append(
            f"            let py_event = match file_event_to_py(py, {file_event_param.name}) {{"
        )
        lines.append("                Ok(ev) => ev,")
        lines.append("                Err(_) => return,")
        lines.append("            };")
        lines.append("            let hook = self.inner.bind(py);")
        lines.append(f'            if let Ok(method) = hook.getattr("{name}") {{')
        lines.append("                let _ = method.call1((py_event,));")
        lines.append("            }")
        lines.append("        });")
        lines.append("    }")
        return lines

    # Determine the method body pattern from return type
    if "Result<(), PyErr>" in ret:
        # Pre-hook: call method, propagate error. Graceful if method missing.
        tuple_arg = (
            f"({params[0].name},)" if len(params) == 1 else f"({', '.join(p.name for p in params)})"
        )
        lines.append(f"    fn {name}({sig}) -> {ret} {{")
        lines.append("        Python::attach(|py| {")
        lines.append("            let hook = self.inner.bind(py);")
        lines.append(f'            if let Ok(method) = hook.getattr("{name}") {{')
        lines.append(f"                method.call1({tuple_arg})?;")
        lines.append("            }")
        lines.append("            Ok(())")
        lines.append("        })")
        lines.append("    }")
    elif ret.strip() == "" or "()" in ret and "Result" not in ret and "Option" not in ret:
        # Fire-and-forget post-hook or observer: call, ignore errors
        tuple_arg = (
            f"({params[0].name},)" if len(params) == 1 else f"({', '.join(p.name for p in params)})"
        )
        lines.append(f"    fn {name}({sig}) {{")
        lines.append("        Python::attach(|py| {")
        lines.append("            let hook = self.inner.bind(py);")
        lines.append(f'            if let Ok(method) = hook.getattr("{name}") {{')
        lines.append(f"                let _ = method.call1({tuple_arg});")
        lines.append("            }")
        lines.append("        });")
        lines.append("    }")
    elif "Option<Vec<u8>>" in ret:
        # try_read: call_method1, extract Vec<u8>, None on error/None
        lines.append(f"    fn {name}({sig}) -> {ret} {{")
        lines.append("        Python::attach(|py| {")
        lines.append(
            f'            let result = self.inner.call_method1(py, "{name}", ({params[0].name},)).ok()?;'
        )
        lines.append("            if result.is_none(py) {")
        lines.append("                return None;")
        lines.append("            }")
        lines.append("            result.extract::<Vec<u8>>(py).ok()")
        lines.append("        })")
        lines.append("    }")
    elif "Option<()>" in ret:
        # try_write/try_delete: call_method1, Some(()) on success
        tuple_arg = (
            f"({params[0].name},)" if len(params) == 1 else f"({', '.join(p.name for p in params)})"
        )
        # Generate single or multiline based on call length
        call_line = f'self.inner.call_method1(py, "{name}", {tuple_arg}).ok()?;'
        lines.append(f"    fn {name}({sig}) -> {ret} {{")
        lines.append("        Python::attach(|py| {")
        if len(call_line) + 12 <= 70:
            lines.append(f"            {call_line}")
        else:
            lines.append("            self.inner")
            lines.append(f'                .call_method1(py, "{name}", {tuple_arg})')
            lines.append("                .ok()?;")
        lines.append("            Some(())")
        lines.append("        })")
        lines.append("    }")
    else:
        # Fallback: fire-and-forget
        tuple_arg = (
            f"({params[0].name},)" if len(params) == 1 else f"({', '.join(p.name for p in params)})"
        )
        lines.append(f"    fn {name}({sig}) {{")
        lines.append("        Python::attach(|py| {")
        lines.append(f'            let _ = self.inner.call_method1(py, "{name}", {tuple_arg});')
        lines.append("        });")
        lines.append("    }")

    return lines


def generate_dispatch_adapters(traits: list[TraitDef]) -> str:
    """Generate Direction 2 (DISPATCH) adapter Rust code from parsed traits."""
    trait_map = {t.name: t for t in traits}

    lines = [
        RUST_MARKER,
        "//! Direction 2 (DISPATCH) adapters — wrap Python hooks/resolvers/observers → Rust traits.",
        "//!",
        "//! These adapters are permanent (hooks are always user-provided Python).",
        "//! Kernel stores Box<dyn InterceptHook>; never knows about Python.",
        "//!",
        "//! Re-generate: python scripts/codegen_kernel_abi.py",
        "",
        "#![allow(dead_code)]",
        "",
        "use pyo3::prelude::*;",
        "",
        "use crate::dispatch::{InterceptHook, PathResolver};",
    ]

    for trait_name, config in DISPATCH_ADAPTERS.items():
        trait = trait_map.get(trait_name)
        if trait is None:
            continue

        adapter_name = config["adapter"]
        cached_name = config.get("cached_name", "")

        lines.append("")
        lines.append(f"// ── {adapter_name} " + "─" * (60 - len(adapter_name)))
        lines.append("")
        lines.append(f"/// Wraps Python → Rust `{trait_name}` trait.")
        lines.append(f"pub(crate) struct {adapter_name} {{")
        lines.append("    inner: Py<PyAny>,")
        if cached_name:
            lines.append(f"    {cached_name}: String,")
        lines.append("}")
        lines.append("")
        lines.append(f"unsafe impl Send for {adapter_name} {{}}")
        lines.append(f"unsafe impl Sync for {adapter_name} {{}}")

        # Constructor
        if cached_name:
            lines.append("")
            lines.append(f"impl {adapter_name} {{")
            lines.append("    pub(crate) fn new(py: Python<'_>, hook: Py<PyAny>) -> Self {")
            lines.append("        let name = hook")
            lines.append("            .bind(py)")
            lines.append('            .getattr("name")')
            lines.append("            .and_then(|n| n.extract::<String>())")
            lines.append('            .unwrap_or_else(|_| "<hook>".to_string());')
            lines.append("        Self {")
            lines.append("            inner: hook,")
            lines.append(f"            {cached_name}: name,")
            lines.append("        }")
            lines.append("    }")
            lines.append("}")

        # Trait impl
        lines.append("")
        lines.append(f"impl {trait_name} for {adapter_name} {{")
        for method in trait.methods:
            method_lines = _gen_dispatch_method(trait_name, method)
            lines.extend(method_lines)
            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()
        lines.append("}")

    lines.append("")
    return "\n".join(lines)


# ── generated_pyo3.rs assembler (PR 21) ──────────────────────────


def _store_adapter_bodies(traits: list[TraitDef]) -> list[str]:
    """Generate Direction 3 (PILLAR) adapter struct+impl blocks (no file header)."""
    trait_map = {t.name: t for t in traits}
    lines: list[str] = []

    for trait_name, config in PILLAR_ADAPTERS.items():
        trait = trait_map.get(trait_name)
        if trait is None:
            continue

        adapter_name = config["adapter"]
        cached_name = config.get("cached_name", "")

        lines.append("")
        lines.append(f"// ── {adapter_name} " + "─" * (60 - len(adapter_name)))
        lines.append("")
        lines.append(f"/// Wraps Python `{trait_name}ABC` -> Rust `{trait_name}` trait.")
        lines.append("///")
        lines.append("/// Transitional adapter: Python backend via GIL (cold path).")
        lines.append(f"pub(crate) struct {adapter_name} {{")
        lines.append("    inner: Py<PyAny>,")
        if cached_name:
            lines.append(f"    {cached_name}: String,")
        lines.append("}")
        lines.append("")
        lines.append(f"unsafe impl Send for {adapter_name} {{}}")
        lines.append(f"unsafe impl Sync for {adapter_name} {{}}")

        # Constructor
        lines.append("")
        lines.append(f"impl {adapter_name} {{")
        if cached_name:
            lines.append("    pub(crate) fn new(py: Python<'_>, inner: Py<PyAny>) -> Self {")
            lines.append("        let name = inner")
            lines.append("            .bind(py)")
            lines.append('            .getattr("name")')
            lines.append("            .and_then(|n| n.extract::<String>())")
            lines.append('            .unwrap_or_else(|_| "<backend>".to_string());')
            lines.append("        Self {")
            lines.append("            inner,")
            lines.append(f"            {cached_name}: name,")
            lines.append("        }")
            lines.append("    }")
        else:
            lines.append("    pub(crate) fn new(inner: Py<PyAny>) -> Self {")
            lines.append("        Self { inner }")
            lines.append("    }")
        lines.append("}")

        # Trait impl
        skip_methods = config.get("skip_methods", set())
        lines.append("")
        lines.append(f"impl {trait_name} for {adapter_name} {{")
        for method in trait.methods:
            if method.name in skip_methods:
                continue
            method_lines = _generate_adapter_method(trait_name, method, config)
            lines.extend(method_lines)
            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()
        lines.append("}")

    return lines


def _dispatch_adapter_bodies(traits: list[TraitDef]) -> list[str]:
    """Generate Direction 2 (DISPATCH) adapter struct+impl blocks (no file header).

    All adapter structs are gated behind ``#[cfg(feature = "py-hook-adapters")]``
    (§11 Phase 14). The feature is default-on; disable to compile out all
    Python-to-Rust hook/resolver/observer bridging.
    """
    trait_map = {t.name: t for t in traits}
    lines: list[str] = []

    # ── Boundary helper: Rust FileEvent → Python FileEvent ────────────
    #
    # MutationObserver::on_mutation takes `&FileEvent` (Rust struct in
    # dispatch.rs). The PyMutationObserverAdapter must convert it to a
    # Python `nexus.core.file_events.FileEvent` (frozen dataclass) so the
    # observer's `on_mutation(event)` Python method receives the right
    # type. We import the class once via OnceLock — subsequent calls hit
    # an unbind()/bind() pair (~ns) and skip the import lookup.
    #
    # Lives here (and not in dispatch.rs) because dispatch.rs is pure
    # Rust with zero PyO3 dependency.
    needs_file_event_helper = any(
        any(p.rust_type == "&FileEvent" for p in m.params)
        for trait_name in DISPATCH_ADAPTERS
        for t in [trait_map.get(trait_name)]
        if t is not None
        for m in t.methods
    )
    if needs_file_event_helper:
        lines.extend(
            [
                "",
                "// ── file_event_to_py — Rust FileEvent → Python FileEvent ──────────",
                "//",
                "// Converts a Rust `crate::dispatch::FileEvent` to a Python",
                "// `nexus.core.file_events.FileEvent` (frozen dataclass). The class",
                "// is cached once via OnceLock — subsequent calls skip the import.",
                "// Used by `PyMutationObserverAdapter::on_mutation` to bridge the",
                "// kernel-internal struct to user observer code.",
                "",
                "static FILE_EVENT_CLASS: std::sync::OnceLock<Option<Py<PyAny>>> =",
                "    std::sync::OnceLock::new();",
                "",
                "fn file_event_class(py: Python<'_>) -> Option<&'static Py<PyAny>> {",
                "    FILE_EVENT_CLASS",
                "        .get_or_init(|| {",
                '            let m = py.import("nexus.core.file_events").ok()?;',
                '            let cls = m.getattr("FileEvent").ok()?.unbind();',
                "            Some(cls)",
                "        })",
                "        .as_ref()",
                "}",
                "",
                "fn file_event_to_py(",
                "    py: Python<'_>,",
                "    event: &crate::dispatch::FileEvent,",
                ") -> PyResult<Py<PyAny>> {",
                "    let cls = file_event_class(py).ok_or_else(|| {",
                "        pyo3::exceptions::PyRuntimeError::new_err(",
                '            "FileEvent class not importable from nexus.core.file_events",',
                "        )",
                "    })?;",
                "    let kwargs = PyDict::new(py);",
                "    // type is the StrEnum string value — Python's __init__ accepts",
                "    // either FileEventType enum or its string value.",
                '    kwargs.set_item("type", event.event_type.as_str())?;',
                '    kwargs.set_item("path", &event.path)?;',
                '    kwargs.set_item("zone_id", event.zone_id.as_deref())?;',
                '    kwargs.set_item("timestamp", &event.timestamp)?;',
                '    kwargs.set_item("event_id", &event.event_id)?;',
                '    kwargs.set_item("old_path", event.old_path.as_deref())?;',
                '    kwargs.set_item("size", event.size)?;',
                '    kwargs.set_item("content_id", event.content_id.as_deref())?;',
                '    kwargs.set_item("agent_id", event.agent_id.as_deref())?;',
                '    kwargs.set_item("vector_clock", event.vector_clock.as_deref())?;',
                '    kwargs.set_item("sequence_number", event.sequence_number)?;',
                '    kwargs.set_item("user_id", event.user_id.as_deref())?;',
                '    kwargs.set_item("version", event.version)?;',
                '    kwargs.set_item("is_new", event.is_new)?;',
                '    kwargs.set_item("new_path", event.new_path.as_deref())?;',
                '    kwargs.set_item("old_content_id", event.old_content_id.as_deref())?;',
                "    let obj = cls.bind(py).call((), Some(&kwargs))?;",
                "    Ok(obj.unbind())",
                "}",
            ]
        )

    for trait_name, config in DISPATCH_ADAPTERS.items():
        trait = trait_map.get(trait_name)
        if trait is None:
            continue

        adapter_name = config["adapter"]
        cached_name = config.get("cached_name", "")

        lines.append("")
        lines.append(f"// ── {adapter_name} " + "─" * (60 - len(adapter_name)))
        lines.append("")
        lines.append(f"/// Wraps Python -> Rust `{trait_name}` trait.")
        lines.append('#[cfg(feature = "py-hook-adapters")]')
        lines.append(f"pub(crate) struct {adapter_name} {{")
        lines.append("    inner: Py<PyAny>,")
        if cached_name:
            lines.append(f"    {cached_name}: String,")
        lines.append("}")
        lines.append("")
        lines.append('#[cfg(feature = "py-hook-adapters")]')
        lines.append(f"unsafe impl Send for {adapter_name} {{}}")
        lines.append('#[cfg(feature = "py-hook-adapters")]')
        lines.append(f"unsafe impl Sync for {adapter_name} {{}}")

        # Constructor (only for those with cached_name)
        if cached_name:
            lines.append("")
            lines.append('#[cfg(feature = "py-hook-adapters")]')
            lines.append(f"impl {adapter_name} {{")
            lines.append("    pub(crate) fn new(py: Python<'_>, hook: Py<PyAny>) -> Self {")
            lines.append("        let name = hook")
            lines.append("            .bind(py)")
            lines.append('            .getattr("name")')
            lines.append("            .and_then(|n| n.extract::<String>())")
            lines.append('            .unwrap_or_else(|_| "<hook>".to_string());')
            lines.append("        Self {")
            lines.append("            inner: hook,")
            lines.append(f"            {cached_name}: name,")
            lines.append("        }")
            lines.append("    }")
            lines.append("}")

        # Trait impl
        lines.append("")
        lines.append('#[cfg(feature = "py-hook-adapters")]')
        lines.append(f"impl {trait_name} for {adapter_name} {{")
        for method in trait.methods:
            method_lines = _gen_dispatch_method(trait_name, method)
            lines.extend(method_lines)
            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()
        lines.append("}")

    return lines


def generate_pyo3_rs(traits: list[TraitDef]) -> str:
    """Generate the entire generated_pyo3.rs — single PyO3 transport file.

    Assembles 8 sections:
      1. Header + imports (template)
      2. Error conversion KernelError → PyErr (template)
      3. Type-bridge helpers (template)
      4. Store adapters — Direction 3 (dynamic, from Rust traits)
      5. Dispatch adapters — Direction 2 (dynamic, from Rust traits)
      6. PyO3 wrapper types (template)
      7. PyKernel struct + #[pymethods] (template, with ctx bug fix)
      8. Private hook dispatch impl (template)
    """
    lines: list[str] = []

    # ── Section 1: Header + imports ────────────────────────────────────
    lines.extend(
        [
            RUST_MARKER,
            "//! PyO3 transport wrapper — bridges Python <-> pure Rust kernel.",
            "//!",
            "//! This single file contains ALL PyO3 code for the kernel boundary:",
            "//!   - Direction 1 (WRAPPER): PyKernel wraps pure Rust Kernel",
            "//!   - Direction 2 (DISPATCH): Python hooks/resolvers/observers -> Rust traits",
            "//!   - Direction 3 (PILLAR): Python storage backends -> Rust trait implementations",
            "//!   - Error conversion: KernelError -> PyErr",
            "//!   - FileMetadata conversion helpers",
            "//!",
            "//! Re-generate: python scripts/codegen_kernel_abi.py",
            "",
            "#![allow(dead_code)]",
            "",
            "use parking_lot::RwLock;",
            "use pyo3::prelude::*;",
            "// Brings ``read_unconditional`` / ``read_yielding_to_writer`` into scope",
            "// — see ``RwLockExt`` in ``kernel.rs`` for the rationale.",
            "use crate::kernel::RwLockExt;",
            "use pyo3::types::{PyBytes, PyDict, PyList};",
            "use std::sync::Arc;",
            "",
            '#[cfg(feature = "py-hook-adapters")]',
            "use crate::dispatch::PathResolver;",
            "use crate::hook_registry::HookRegistry;",
            '#[cfg(feature = "py-hook-adapters")]',
            "use crate::hook_registry::InterceptHook;",
            "use crate::kernel::{Kernel, KernelError, OperationContext, WriteBufferFlushHandle};",
            "use crate::meta_store::{FileMetadata, MetaStoreError};",
            "use crate::vfs_router::RouteError;",
        ]
    )

    # ── Section 1b: PyServiceLifecycle adapter + run_coro helper ───────
    lines.extend(
        [
            "",
            "// ═══════════════════════════════════════════════════════════════════════════",
            "// PyServiceLifecycle — Python → ServiceLifecycle adapter",
            "// ═══════════════════════════════════════════════════════════════════════════",
            "",
            "use crate::service_registry::ServiceLifecycle;",
            "",
            "/// Wraps a `Py<PyAny>` Python service instance into the language-agnostic",
            "/// `ServiceLifecycle` trait. All Python asyncio / BackgroundService",
            "/// logic stays here in the cdylib layer — the kernel never touches PyO3",
            "/// for service management.",
            "struct PyServiceLifecycle(Py<PyAny>);",
            "",
            "/// Run a Python coroutine to completion via stdlib asyncio.",
            "fn run_coro(py: Python<'_>, coro: &Bound<'_, PyAny>, timeout_secs: f64) -> PyResult<()> {",
            '    let asyncio = py.import("asyncio")?;',
            '    let timed = asyncio.call_method1("wait_for", (coro, timeout_secs))?;',
            '    asyncio.call_method1("run", (&timed,))?;',
            "    Ok(())",
            "}",
            "",
            "impl ServiceLifecycle for PyServiceLifecycle {",
            "    fn start(&self, timeout_secs: f64) -> Result<(), String> {",
            "        Python::attach(|py| {",
            "            let instance = self.0.bind(py);",
            "            let bg_cls = py",
            '                .import("nexus.contracts.protocols.service_lifecycle")',
            '                .and_then(|m| m.getattr("BackgroundService"))',
            '                .map_err(|e| format!("{e}"))?;',
            '            if !instance.is_instance(&bg_cls).map_err(|e| format!("{e}"))? {',
            "                return Ok(());",
            "            }",
            '            let coro = instance.call_method0("start").map_err(|e| format!("{e}"))?;',
            '            run_coro(py, &coro, timeout_secs).map_err(|e| format!("{e}")',
            ")",
            "        })",
            "    }",
            "",
            "    fn stop(&self, timeout_secs: f64) -> Result<(), String> {",
            "        Python::attach(|py| {",
            "            let instance = self.0.bind(py);",
            "            let bg_cls = py",
            '                .import("nexus.contracts.protocols.service_lifecycle")',
            '                .and_then(|m| m.getattr("BackgroundService"))',
            '                .map_err(|e| format!("{e}"))?;',
            '            if !instance.is_instance(&bg_cls).map_err(|e| format!("{e}"))? {',
            "                return Ok(());",
            "            }",
            '            let coro = instance.call_method0("stop").map_err(|e| format!("{e}"))?;',
            '            run_coro(py, &coro, timeout_secs).map_err(|e| format!("{e}")',
            ")",
            "        })",
            "    }",
            "",
            "    fn close(&self) -> Result<(), String> {",
            "        Python::attach(|py| {",
            "            let instance = self.0.bind(py);",
            '            if let Ok(close_fn) = instance.getattr("close") {',
            "                if close_fn.is_callable() {",
            '                    close_fn.call0().map_err(|e| format!("{e}"))?;',
            "                }",
            "            }",
            "            Ok(())",
            "        })",
            "    }",
            "",
            "    fn type_name(&self) -> String {",
            "        Python::attach(|py| {",
            "            self.0",
            "                .bind(py)",
            "                .get_type()",
            "                .name()",
            "                .map(|n| n.to_string())",
            '                .unwrap_or_else(|_| "?".to_string())',
            "        })",
            "    }",
            "",
            "    fn clone_box(&self) -> Box<dyn ServiceLifecycle> {",
            "        Python::attach(|py| Box::new(PyServiceLifecycle(self.0.clone_ref(py))))",
            "    }",
            "}",
        ]
    )

    # ── Section 2: Error conversion (with cached exception classes) ─────
    lines.extend(
        [
            "",
            "// ═══════════════════════════════════════════════════════════════════════════",
            "// Error conversion: KernelError -> PyErr (cached exception classes)",
            "// ═══════════════════════════════════════════════════════════════════════════",
            "",
            "/// Cached exception class references — initialized once on first use.",
            "struct ExceptionCache {",
            "    invalid_path: Py<PyAny>,",
            "    file_not_found: Py<PyAny>,",
            "    backend_error: Py<PyAny>,",
            "    permission_denied: Py<PyAny>,",
            "}",
            "",
            "static EXCEPTION_CACHE: std::sync::OnceLock<Option<ExceptionCache>> = std::sync::OnceLock::new();",
            "",
            "fn get_exception_cache(py: Python<'_>) -> Option<&'static ExceptionCache> {",
            "    EXCEPTION_CACHE",
            "        .get_or_init(|| {",
            '            let m = py.import("nexus.contracts.exceptions").ok()?;',
            '            let invalid_path = m.getattr("InvalidPathError").ok()?.unbind();',
            '            let file_not_found = m.getattr("NexusFileNotFoundError").ok()?.unbind();',
            '            let backend_error = m.getattr("BackendError").ok()?.unbind();',
            '            let permission_denied = m.getattr("PermissionDeniedError").ok()?.unbind();',
            "            Some(ExceptionCache {",
            "                invalid_path,",
            "                file_not_found,",
            "                backend_error,",
            "                permission_denied,",
            "            })",
            "        })",
            "        .as_ref()",
            "}",
            "",
            "impl From<KernelError> for PyErr {",
            "    fn from(e: KernelError) -> PyErr {",
            "        match e {",
            "            KernelError::InvalidPath(msg) => Python::attach(|py| {",
            "                if let Some(cache) = get_exception_cache(py) {",
            "                    cache",
            "                        .invalid_path",
            "                        .bind(py)",
            "                        .call1((&msg,))",
            "                        .map(PyErr::from_value)",
            "                        .unwrap_or_else(|_| pyo3::exceptions::PyValueError::new_err(msg))",
            "                } else {",
            "                    pyo3::exceptions::PyValueError::new_err(msg)",
            "                }",
            "            }),",
            "            KernelError::FileNotFound(path) => Python::attach(|py| {",
            "                if let Some(cache) = get_exception_cache(py) {",
            "                    cache",
            "                        .file_not_found",
            "                        .bind(py)",
            "                        .call1((&path,))",
            "                        .map(PyErr::from_value)",
            "                        .unwrap_or_else(|_| pyo3::exceptions::PyFileNotFoundError::new_err(path))",
            "                } else {",
            "                    pyo3::exceptions::PyFileNotFoundError::new_err(path)",
            "                }",
            "            }),",
            "            KernelError::FileExists(msg) => pyo3::exceptions::PyFileExistsError::new_err(msg),",
            "            KernelError::Route(RouteError::NotMounted(msg)) => {",
            "                pyo3::exceptions::PyValueError::new_err(msg)",
            "            }",
            "            KernelError::IOError(msg) => pyo3::exceptions::PyIOError::new_err(msg),",
            "            KernelError::TrieError(msg) => pyo3::exceptions::PyValueError::new_err(msg),",
            "            // IPC error variants",
            '            KernelError::PipeFull(msg) => pyo3::exceptions::PyRuntimeError::new_err(format!("PipeFull:{msg}")),',
            '            KernelError::PipeEmpty(msg) => pyo3::exceptions::PyRuntimeError::new_err(format!("PipeEmpty:{msg}")),',
            '            KernelError::PipeClosed(msg) => pyo3::exceptions::PyRuntimeError::new_err(format!("PipeClosed:{msg}")),',
            '            KernelError::PipeExists(msg) => pyo3::exceptions::PyRuntimeError::new_err(format!("PipeExists:{msg}")),',
            "            KernelError::PipeNotFound(path) => Python::attach(|py| {",
            "                if let Some(cache) = get_exception_cache(py) {",
            "                    cache",
            "                        .file_not_found",
            "                        .bind(py)",
            "                        .call1((&path,))",
            "                        .map(PyErr::from_value)",
            "                        .unwrap_or_else(|_| pyo3::exceptions::PyFileNotFoundError::new_err(path))",
            "                } else {",
            "                    pyo3::exceptions::PyFileNotFoundError::new_err(path)",
            "                }",
            "            }),",
            '            KernelError::StreamFull(msg) => pyo3::exceptions::PyRuntimeError::new_err(format!("StreamFull:{msg}")),',
            '            KernelError::StreamEmpty(msg) => pyo3::exceptions::PyRuntimeError::new_err(format!("StreamEmpty:{msg}")),',
            '            KernelError::StreamClosed(msg) => pyo3::exceptions::PyRuntimeError::new_err(format!("StreamClosed:{msg}")),',
            '            KernelError::StreamExists(msg) => pyo3::exceptions::PyRuntimeError::new_err(format!("StreamExists:{msg}")),',
            "            KernelError::StreamNotFound(path) => Python::attach(|py| {",
            "                if let Some(cache) = get_exception_cache(py) {",
            "                    cache",
            "                        .file_not_found",
            "                        .bind(py)",
            "                        .call1((&path,))",
            "                        .map(PyErr::from_value)",
            "                        .unwrap_or_else(|_| pyo3::exceptions::PyFileNotFoundError::new_err(path))",
            "                } else {",
            "                    pyo3::exceptions::PyFileNotFoundError::new_err(path)",
            "                }",
            "            }),",
            '            KernelError::WouldBlock(msg) => pyo3::exceptions::PyRuntimeError::new_err(format!("WouldBlock:{msg}")),',
            "            KernelError::PermissionDenied(msg) => pyo3::exceptions::PyPermissionError::new_err(msg),",
            "            KernelError::BackendError(msg) => Python::attach(|py| {",
            "                if let Some(cache) = get_exception_cache(py) {",
            "                    cache",
            "                        .backend_error",
            "                        .bind(py)",
            "                        .call1((&msg,))",
            "                        .map(PyErr::from_value)",
            "                        .unwrap_or_else(|_| pyo3::exceptions::PyIOError::new_err(msg))",
            "                } else {",
            "                    pyo3::exceptions::PyIOError::new_err(msg)",
            "                }",
            "            }),",
            '            KernelError::Federation(msg) => pyo3::exceptions::PyRuntimeError::new_err(format!("Federation:{msg}")),',
            "        }",
            "    }",
            "}",
            "",
            "// ═══════════════════════════════════════════════════════════════════════════",
            "// Hook context class cache — OnceLock avoids py.import() per syscall (~1μs)",
            "// ═══════════════════════════════════════════════════════════════════════════",
            "",
            "/// Cached hook context class references — initialized once on first hooked syscall.",
            "struct HookContextCache {",
            "    read: Py<PyAny>,",
            "    write: Py<PyAny>,",
            "    delete: Py<PyAny>,",
            "    rename: Py<PyAny>,",
            "    mkdir: Py<PyAny>,",
            "    rmdir: Py<PyAny>,",
            "    copy: Py<PyAny>,",
            "    stat: Py<PyAny>,",
            "    access: Py<PyAny>,",
            "}",
            "",
            "static HOOK_CTX_CACHE: std::sync::OnceLock<Option<HookContextCache>> = std::sync::OnceLock::new();",
            "",
            "fn get_hook_ctx_cache(py: Python<'_>) -> Option<&'static HookContextCache> {",
            "    HOOK_CTX_CACHE",
            "        .get_or_init(|| {",
            '            let m = py.import("nexus.contracts.vfs_hooks").ok()?;',
            "            Some(HookContextCache {",
            '                read: m.getattr("ReadHookContext").ok()?.unbind(),',
            '                write: m.getattr("WriteHookContext").ok()?.unbind(),',
            '                delete: m.getattr("DeleteHookContext").ok()?.unbind(),',
            '                rename: m.getattr("RenameHookContext").ok()?.unbind(),',
            '                mkdir: m.getattr("MkdirHookContext").ok()?.unbind(),',
            '                rmdir: m.getattr("RmdirHookContext").ok()?.unbind(),',
            '                copy: m.getattr("CopyHookContext").ok()?.unbind(),',
            '                stat: m.getattr("StatHookContext").ok()?.unbind(),',
            '                access: m.getattr("AccessHookContext").ok()?.unbind(),',
            "            })",
            "        })",
            "        .as_ref()",
            "}",
        ]
    )

    # ── Section 3: Type-bridge helpers ─────────────────────────────────
    lines.extend(
        [
            "",
            "// ═══════════════════════════════════════════════════════════════════════════",
            "// FileMetadata conversion helpers (from generated_store.rs)",
            "// ═══════════════════════════════════════════════════════════════════════════",
            "",
            "/// Extract Rust FileMetadata from a Python FileMetadata object.",
            "fn extract_metadata(",
            "    py: Python<'_>,",
            "    obj: &Bound<'_, PyAny>,",
            ") -> Result<FileMetadata, MetaStoreError> {",
            "    let get_str = |name: &str| -> Result<String, MetaStoreError> {",
            "        obj.getattr(name)",
            "            .and_then(|v| v.extract::<String>())",
            '            .map_err(|e| MetaStoreError::IOError(format!("field {name}: {e}")))',
            "    };",
            "    let get_opt_str = |name: &str| -> Result<Option<String>, MetaStoreError> {",
            "        match obj.getattr(name) {",
            "            Ok(v) if v.is_none() => Ok(None),",
            "            Ok(v) => v",
            "                .extract::<String>()",
            "                .map(Some)",
            '                .map_err(|e| MetaStoreError::IOError(format!("field {name}: {e}"))),',
            '            Err(e) => Err(MetaStoreError::IOError(format!("field {name}: {e}"))),',
            "        }",
            "    };",
            "    let get_u64_or_zero = |name: &str| -> Result<u64, MetaStoreError> {",
            "        match obj.getattr(name) {",
            "            Ok(v) => v",
            "                .extract::<u64>()",
            '                .map_err(|e| MetaStoreError::IOError(format!("field {name}: {e}"))),',
            "            Err(_) => Ok(0),",
            "        }",
            "    };",
            "",
            "    let _ = py;",
            "    Ok(FileMetadata {",
            '        path: get_str("path")?,',
            "        size: obj",
            '            .getattr("size")',
            "            .and_then(|v| v.extract::<u64>())",
            '            .map_err(|e| MetaStoreError::IOError(format!("field size: {e}")))?,',
            '        content_id: get_opt_str("content_id")?,',
            '        gen: get_u64_or_zero("gen")?,',
            "        version: obj",
            '            .getattr("version")',
            "            .and_then(|v| v.extract::<u32>())",
            '            .map_err(|e| MetaStoreError::IOError(format!("field version: {e}")))?,',
            "        entry_type: obj",
            '            .getattr("entry_type")',
            "            .and_then(|v| v.extract::<u8>())",
            '            .map_err(|e| MetaStoreError::IOError(format!("field entry_type: {e}")))?,',
            '        zone_id: get_opt_str("zone_id")?,',
            '        mime_type: get_opt_str("mime_type")?,',
            '        created_at_ms: extract_opt_datetime_ms(obj, "created_at"),',
            '        modified_at_ms: extract_opt_datetime_ms(obj, "modified_at"),',
            '        last_writer_address: get_opt_str("last_writer_address")?,',
            '        target_zone_id: get_opt_str("target_zone_id")?,',
            '        link_target: get_opt_str("link_target").ok().flatten(),',
            "    })",
            "}",
            "",
            "/// Convert a Python ``datetime`` attribute to epoch milliseconds.",
            "/// Returns None if the attribute is missing or not a datetime.",
            "fn extract_opt_datetime_ms(obj: &Bound<'_, PyAny>, name: &str) -> Option<i64> {",
            "    let v = obj.getattr(name).ok()?;",
            "    if v.is_none() {",
            "        return None;",
            "    }",
            '    let ts = v.call_method0("timestamp").ok()?;',
            "    let secs = ts.extract::<f64>().ok()?;",
            "    Some((secs * 1000.0) as i64)",
            "}",
            "",
            "/// Convert Rust FileMetadata -> Python FileMetadata (for metastore.put()).",
            "fn to_python_metadata<'py>(",
            "    py: Python<'py>,",
            "    meta: &FileMetadata,",
            ") -> Result<Bound<'py, PyAny>, MetaStoreError> {",
            "    fn err(e: PyErr) -> MetaStoreError {",
            '        MetaStoreError::IOError(format!("to_python_metadata: {e}"))',
            "    }",
            "    let cls = py",
            '        .import("nexus.contracts.metadata")',
            "        .map_err(err)?",
            '        .getattr("FileMetadata")',
            "        .map_err(err)?;",
            "    let kwargs = PyDict::new(py);",
            '    kwargs.set_item("path", &meta.path).map_err(err)?;',
            '    kwargs.set_item("size", meta.size).map_err(err)?;',
            '    kwargs.set_item("content_id", meta.content_id.as_deref()).map_err(err)?;',
            '    kwargs.set_item("gen", meta.gen).map_err(err)?;',
            '    kwargs.set_item("version", meta.version).map_err(err)?;',
            "    kwargs",
            '        .set_item("entry_type", meta.entry_type)',
            "        .map_err(err)?;",
            "    kwargs",
            '        .set_item("zone_id", meta.zone_id.as_deref())',
            "        .map_err(err)?;",
            "    kwargs",
            '        .set_item("mime_type", meta.mime_type.as_deref())',
            "        .map_err(err)?;",
            '    set_optional_datetime(py, &kwargs, "created_at", meta.created_at_ms).map_err(err)?;',
            '    set_optional_datetime(py, &kwargs, "modified_at", meta.modified_at_ms).map_err(err)?;',
            "    if let Some(target) = meta.link_target.as_deref() {",
            '        kwargs.set_item("link_target", target).map_err(err)?;',
            "    }",
            "    cls.call((), Some(&kwargs)).map_err(err)",
            "}",
            "",
            "/// Set a kwargs entry to a UTC datetime built from epoch ms (or None).",
            "fn set_optional_datetime(",
            "    py: Python<'_>,",
            "    kwargs: &Bound<'_, PyDict>,",
            "    key: &str,",
            "    ms: Option<i64>,",
            ") -> PyResult<()> {",
            "    let Some(ms) = ms else {",
            "        return kwargs.set_item(key, py.None());",
            "    };",
            '    let datetime = py.import("datetime")?;',
            '    let utc = datetime.getattr("timezone")?.getattr("utc")?;',
            '    let from_ts = datetime.getattr("datetime")?.getattr("fromtimestamp")?;',
            "    let secs = ms as f64 / 1000.0;",
            "    let dt = from_ts.call((secs, &utc), None)?;",
            "    kwargs.set_item(key, dt)",
            "}",
            "",
            "/// Set a stat-dict entry to a UTC ISO-8601 string built from epoch ms.",
            "/// Pure Rust (chrono) — no Python crossing.",
            "fn set_optional_iso_datetime(",
            "    py: Python<'_>,",
            "    dict: &Bound<'_, PyDict>,",
            "    key: &str,",
            "    ms: Option<i64>,",
            ") -> PyResult<()> {",
            "    let Some(ms) = ms else {",
            "        return dict.set_item(key, py.None());",
            "    };",
            "    let secs = ms / 1000;",
            "    let nsecs = ((ms % 1000) * 1_000_000) as u32;",
            "    if let Some(dt) = chrono::DateTime::from_timestamp(secs, nsecs) {",
            "        dict.set_item(key, dt.to_rfc3339_opts(chrono::SecondsFormat::Millis, true))",
            "    } else {",
            "        dict.set_item(key, py.None())",
            "    }",
            "}",
            "",
            "/// Convert StatResult → Python dict (same shape as sys_stat output).",
            "fn stat_result_to_pydict<'py>(py: Python<'py>, s: &crate::kernel::StatResult) -> Bound<'py, PyDict> {",
            "    let dict = PyDict::new(py);",
            '    let _ = dict.set_item("path", &s.path);',
            '    let _ = dict.set_item("size", s.size);',
            '    let _ = dict.set_item("last_writer_address", s.last_writer_address.as_deref());',
            '    let _ = dict.set_item("content_id", s.content_id.as_deref());',
            '    let _ = dict.set_item("mime_type", &s.mime_type);',
            '    let _ = set_optional_iso_datetime(py, &dict, "created_at", s.created_at_ms);',
            '    let _ = set_optional_iso_datetime(py, &dict, "modified_at", s.modified_at_ms);',
            '    let _ = dict.set_item("is_directory", s.is_directory);',
            '    let _ = dict.set_item("entry_type", s.entry_type);',
            '    let _ = dict.set_item("mode", s.mode);',
            '    let _ = dict.set_item("version", s.version);',
            '    let _ = dict.set_item("zone_id", s.zone_id.as_deref());',
            '    let _ = dict.set_item("link_target", s.link_target.as_deref());',
            '    let _ = dict.set_item("lock", py.None());',
            "    dict",
            "}",
            "",
            "/// Convert Rust OperationContext -> Python OperationContext.",
            "///",
            "/// Forwards ALL fields so Python hooks see the full credential.",
            "fn rust_ctx_to_python<'py>(",
            "    py: Python<'py>,",
            "    ctx: &OperationContext,",
            "    backend_path: &str,",
            ") -> Result<Bound<'py, PyAny>, String> {",
            "    let cls = py",
            '        .import("nexus.contracts.types")',
            '        .and_then(|m| m.getattr("OperationContext"))',
            '        .map_err(|e| format!("import OperationContext: {e}"))?;',
            "    let kwargs = PyDict::new(py);",
            '    let _ = kwargs.set_item("user_id", &ctx.user_id);',
            "    // Use context_zone_id (caller's zone, may be None) for Python context,",
            "    // NOT routing zone_id (always set to NexusFS instance zone).",
            "    match &ctx.context_zone_id {",
            "        Some(z) => {",
            '            let _ = kwargs.set_item("zone_id", z);',
            "        }",
            "        None => {",
            '            let _ = kwargs.set_item("zone_id", py.None());',
            "        }",
            "    }",
            '    let _ = kwargs.set_item("is_admin", ctx.is_admin);',
            '    let _ = kwargs.set_item("is_system", ctx.is_system);',
            '    let _ = kwargs.set_item("backend_path", backend_path);',
            '    let groups = PyList::new(py, &ctx.groups).map_err(|e| format!("groups: {e}"))?;',
            '    let _ = kwargs.set_item("groups", groups);',
            "    if let Some(ref agent_id) = ctx.agent_id {",
            '        let _ = kwargs.set_item("agent_id", agent_id);',
            "    }",
            "    // admin_capabilities: Vec<String> → Python set[str]",
            '    let cap_set = pyo3::types::PySet::empty(py).map_err(|e| format!("set(): {e}"))?;',
            "    for cap in &ctx.admin_capabilities {",
            "        let _ = cap_set.add(cap);",
            "    }",
            '    let _ = kwargs.set_item("admin_capabilities", &cap_set);',
            '    let _ = kwargs.set_item("subject_type", &ctx.subject_type);',
            "    if let Some(ref sid) = ctx.subject_id {",
            '        let _ = kwargs.set_item("subject_id", sid);',
            "    }",
            "    if !ctx.request_id.is_empty() {",
            '        let _ = kwargs.set_item("request_id", &ctx.request_id);',
            "    }",
            "    // zone_perms: Vec<(String, String)> → Python tuple of (zone_id, perm_chars) pairs",
            "    if !ctx.zone_perms.is_empty() {",
            "        let zp = PyList::new(",
            "            py,",
            "            ctx.zone_perms",
            "                .iter()",
            "                .map(|(z, p)| (z.as_str(), p.as_str())),",
            "        )",
            '        .map_err(|e| format!("zone_perms: {e}"))?;',
            '        let _ = kwargs.set_item("zone_perms", zp);',
            "    }",
            "    cls.call((), Some(&kwargs))",
            '        .map_err(|e| format!("OperationContext(): {e}"))',
            "}",
        ]
    )

    # ── Section 4: Store adapters (Direction 3) — dynamic ──────────────
    lines.extend(
        [
            "",
            "// ═══════════════════════════════════════════════════════════════════════════",
            "// Direction 3 (PILLAR): Store adapters — Python ABC -> Rust trait",
            "// ═══════════════════════════════════════════════════════════════════════════",
        ]
    )
    lines.extend(_store_adapter_bodies(traits))

    # ── Section 5: Dispatch adapters (Direction 2) — dynamic ───────────
    lines.extend(
        [
            "",
            "// ═══════════════════════════════════════════════════════════════════════════",
            "// Direction 2 (DISPATCH): Python hooks/resolvers/observers -> Rust traits",
            "// ═══════════════════════════════════════════════════════════════════════════",
        ]
    )
    lines.extend(_dispatch_adapter_bodies(traits))

    # ── Section 6: PyO3 wrapper types ──────────────────────────────────
    lines.extend(
        [
            "",
            "// ── PyPermissionProviderAdapter ──────────────────────────────────────",
            "",
            "use crate::core::dispatch::{Permission, PermissionDecision, PermissionProvider};",
            "",
            "pub(crate) struct PyPermissionProviderAdapter {",
            "    checker: Py<PyAny>,",
            "}",
            "",
            "unsafe impl Send for PyPermissionProviderAdapter {}",
            "",
            "unsafe impl Sync for PyPermissionProviderAdapter {}",
            "",
            "impl PermissionProvider for PyPermissionProviderAdapter {",
            "    fn check(",
            "        &self,",
            "        path: &str,",
            "        permission: Permission,",
            "        _ctx: &contracts::OperationContext,",
            "    ) -> PermissionDecision {",
            "        Python::attach(|py| {",
            "            let checker = self.checker.bind(py);",
            '            let perm_mod = match py.import("nexus.contracts.types") {',
            "                Ok(m) => m,",
            "                Err(_) => return PermissionDecision::Unknown,",
            "            };",
            '            let perm_cls = match perm_mod.getattr("Permission") {',
            "                Ok(c) => c,",
            "                Err(_) => return PermissionDecision::Unknown,",
            "            };",
            "            let py_perm = match perm_cls.getattr(permission.as_str()) {",
            "                Ok(p) => p,",
            "                Err(_) => return PermissionDecision::Unknown,",
            "            };",
            '            let py_ctx = match rust_ctx_to_python(py, _ctx, "") {',
            "                Ok(c) => c,",
            "                Err(_) => return PermissionDecision::Unknown,",
            "            };",
            '            match checker.call_method1("check", (path, py_perm, py_ctx)) {',
            "                Ok(_) => PermissionDecision::Allow,",
            "                Err(_) => PermissionDecision::Deny(format!(",
            "                    \"permission denied: {} on '{}'\",",
            "                    permission.as_str(),",
            "                    path",
            "                )),",
            "            }",
            "        })",
            "    }",
            "}",
            "",
            "// ═══════════════════════════════════════════════════════════════════════════",
            "// Direction 1 (WRAPPER): PyO3 wrapper types",
            "// ═══════════════════════════════════════════════════════════════════════════",
            "",
            "// ── PyOperationContext ──────────────────────────────────────────",
            "",
            "/// Python-facing OperationContext (wraps pure Rust OperationContext).",
            "#[pyclass(get_all, from_py_object)]",
            "#[derive(Clone, Debug)]",
            "pub struct PyOperationContext {",
            "    pub user_id: String,",
            "    pub zone_id: String,",
            "    pub is_admin: bool,",
            "    pub agent_id: Option<String>,",
            "    pub is_system: bool,",
            "    pub groups: Vec<String>,",
            "    pub admin_capabilities: Vec<String>,",
            "    pub subject_type: String,",
            "    pub subject_id: Option<String>,",
            "    pub request_id: String,",
            "    pub context_zone_id: Option<String>,",
            "    pub zone_perms: Vec<(String, String)>,",
            "}",
            "",
            "#[pymethods]",
            "impl PyOperationContext {",
            "    #[new]",
            '    #[pyo3(signature = (user_id="anonymous", zone_id="root", is_admin=false, agent_id=None, is_system=false, groups=vec![], admin_capabilities=vec![], subject_type="user", subject_id=None, request_id="", context_zone_id=None, zone_perms=vec![]))]',
            "    #[allow(clippy::too_many_arguments)]",
            "    fn new(",
            "        user_id: &str,",
            "        zone_id: &str,",
            "        is_admin: bool,",
            "        agent_id: Option<&str>,",
            "        is_system: bool,",
            "        groups: Vec<String>,",
            "        admin_capabilities: Vec<String>,",
            "        subject_type: &str,",
            "        subject_id: Option<&str>,",
            "        request_id: &str,",
            "        context_zone_id: Option<&str>,",
            "        zone_perms: Vec<(String, String)>,",
            "    ) -> Self {",
            "        Self {",
            "            user_id: user_id.to_string(),",
            "            zone_id: zone_id.to_string(),",
            "            is_admin,",
            "            agent_id: agent_id.map(|s| s.to_string()),",
            "            is_system,",
            "            groups,",
            "            admin_capabilities,",
            "            subject_type: subject_type.to_string(),",
            "            subject_id: subject_id.map(|s| s.to_string()),",
            "            request_id: request_id.to_string(),",
            "            context_zone_id: context_zone_id.map(|s| s.to_string()),",
            "            zone_perms,",
            "        }",
            "    }",
            "}",
            "",
            "impl PyOperationContext {",
            "    /// Convert to pure Rust OperationContext for kernel calls.",
            "    fn to_rust(&self) -> OperationContext {",
            "        OperationContext {",
            "            user_id: self.user_id.clone(),",
            "            zone_id: self.zone_id.clone(),",
            "            is_admin: self.is_admin,",
            "            agent_id: self.agent_id.clone(),",
            "            is_system: self.is_system,",
            "            groups: self.groups.clone(),",
            "            admin_capabilities: self.admin_capabilities.clone(),",
            "            subject_type: self.subject_type.clone(),",
            "            subject_id: self.subject_id.clone(),",
            "            request_id: self.request_id.clone(),",
            "            context_zone_id: self.context_zone_id.clone(),",
            "            zone_perms: self.zone_perms.clone(),",
            "        }",
            "    }",
            "}",
            "",
            "// ── PySysReadResult ─────────────────────────────────────────────",
            "",
            "/// Python-facing SysReadResult (data is PyBytes, not Vec<u8>).",
            "#[pyclass(get_all)]",
            "pub struct PySysReadResult {",
            "    pub data: Option<Py<PyBytes>>,",
            "    pub post_hook_needed: bool,",
            "    pub content_id: Option<String>,",
            "    pub gen: u64,",
            "    pub entry_type: u8,",
            "    pub stream_next_offset: Option<usize>,",
            "}",
            "",
            "// ── PySysWriteResult ────────────────────────────────────────────",
            "",
            "/// Python-facing SysWriteResult.",
            "#[pyclass(get_all)]",
            "pub struct PySysWriteResult {",
            "    pub hit: bool,",
            "    pub content_id: Option<String>,",
            "    pub post_hook_needed: bool,",
            "    pub version: u32,",
            "    pub gen: u64,",
            "    pub size: u64,",
            "    pub is_new: bool,",
            "    pub old_content_id: Option<String>,",
            "    pub old_size: Option<u64>,",
            "    pub old_version: Option<u32>,",
            "    pub old_modified_at_ms: Option<i64>,",
            "}",
            "",
            "// ── PyFlushWriteBufferResult ───────────────────────────────────",
            "",
            "/// Python-facing FlushWriteBufferResult.",
            "#[pyclass(get_all)]",
            "pub struct PyFlushWriteBufferResult {",
            "    pub flushed: usize,",
            "    pub failed: usize,",
            "    pub errors: Vec<String>,",
            "}",
            "",
            "// ── PySysUnlinkResult ───────────────────────────────────────────",
            "",
            "/// Python-facing SysUnlinkResult.",
            "#[pyclass(get_all)]",
            "pub struct PySysUnlinkResult {",
            "    pub hit: bool,",
            "    pub entry_type: u8,",
            "    pub post_hook_needed: bool,",
            "    pub path: String,",
            "    pub content_id: Option<String>,",
            "    pub size: u64,",
            "}",
            "",
            "// ── PySysRenameResult ───────────────────────────────────────────",
            "",
            "/// Python-facing SysRenameResult.",
            "#[pyclass(get_all)]",
            "pub struct PySysRenameResult {",
            "    pub hit: bool,",
            "    pub success: bool,",
            "    pub post_hook_needed: bool,",
            "    pub is_directory: bool,",
            "    pub old_content_id: Option<String>,",
            "    pub old_size: Option<u64>,",
            "    pub old_version: Option<u32>,",
            "    pub old_modified_at_ms: Option<i64>,",
            "}",
            "",
            "// ── PySysMkdirResult ────────────────────────────────────────────",
            "",
            "/// Python-facing SysMkdirResult.",
            "#[pyclass(get_all)]",
            "pub struct PySysMkdirResult {",
            "    pub hit: bool,",
            "    pub post_hook_needed: bool,",
            "}",
            "",
            "// ── PySysRmdirResult ────────────────────────────────────────────",
            "",
            "/// Python-facing SysRmdirResult.",
            "#[pyclass(get_all)]",
            "pub struct PySysRmdirResult {",
            "    pub hit: bool,",
            "    pub post_hook_needed: bool,",
            "    pub children_deleted: usize,",
            "}",
            "",
            "// ── PySysCopyResult ─────────────────────────────────────────────",
            "",
            "/// Python-facing SysCopyResult.",
            "#[pyclass(get_all)]",
            "pub struct PySysCopyResult {",
            "    pub hit: bool,",
            "    pub post_hook_needed: bool,",
            "    pub dst_path: String,",
            "    pub content_id: Option<String>,",
            "    pub size: u64,",
            "    pub version: u32,",
            "    pub gen: u64,",
            "}",
            "",
            "// ── PyBatchReadItem ──────────────────────────────────────────────",
            "",
            '/// Per-item result for `sys_read_batch`. `error_kind == ""` means success.',
            "/// On error `data` / `content_id` are `None` and counters are zero.",
            "#[pyclass(get_all)]",
            "pub struct PyBatchReadItem {",
            "    pub data: Option<Py<PyBytes>>,",
            "    pub content_id: Option<String>,",
            "    pub gen: u64,",
            "    pub entry_type: u8,",
            "    pub post_hook_needed: bool,",
            '    /// Empty string on success; one of "not_found" / "permission_denied" /',
            '    /// "invalid_path" / "io_error" on failure.',
            "    pub error_kind: String,",
            "    /// Human-readable error detail; empty string on success.",
            "    pub error_message: String,",
            "}",
        ]
    )

    # ── Section 7: PyKernel ────────────────────────────────────────────
    lines.extend(
        [
            "",
            "// ═══════════════════════════════════════════════════════════════════════════",
            "// PyKernel — wraps pure Rust Kernel + owns Hook registry",
            "// ═══════════════════════════════════════════════════════════════════════════",
            "",
            "/// Python-facing Kernel. Wraps the pure Rust `Kernel` and adds:",
            "///   - Hook registry (PyO3-specific, stored here not in Rust Kernel)",
            "///   - PRE-INTERCEPT dispatch (requires GIL for Python hook contexts)",
            "///   - Type conversion (Vec<u8> -> PyBytes, StatResult -> PyDict, etc.)",
            "#[pyclass]",
            "pub struct PyKernel {",
            "    /// Crate-visible so `grpc_server.rs` (and any other",
            "    /// kernel-internal task spawner) can clone the Arc",
            "    /// without an extra accessor method that codegen would",
            "    /// have to preserve. Not exposed to Python.",
            "    pub(crate) inner: Arc<Kernel>,",
            "    _write_buffer_flusher: WriteBufferFlushHandle,",
            "    // RwLock (not Mutex) so a hook callback can re-enter",
            "    // ``sys_*`` without deadlocking. The recursion is real:",
            "    // ReBAC's permission_hook reads its own ``/__sys__/rebac/...``",
            "    // config via ``sys_read`` during a permission check, which",
            "    // re-enters dispatch_pre_hooks on the same thread. Read sites",
            "    // call ``read_unconditional`` (the ``RwLockExt`` rename for",
            "    // parking_lot's ``read_recursive``) so the recursive shared",
            "    // acquisition always succeeds; writes happen only at hook",
            "    // register / unregister, never during dispatch, so the writer-",
            "    // starvation cost does not apply.",
            "    hooks: RwLock<HookRegistry>,",
            "}",
            "",
            "// Rust-side helpers on PyKernel — NOT exposed to Python (no #[pymethods]).",
            "impl PyKernel {",
            "    /// Compute the kernel's canonical key for a `(mount_point, zone_id)`",
            "    /// pair. Forwards to `Kernel::canonical_mount_key`.",
            "    pub fn canonical_mount_key(mount_point: &str, zone_id: &str) -> String {",
            "        Kernel::canonical_mount_key(mount_point, zone_id)",
            "    }",
            "",
            "    /// Borrow the inner `&Kernel`.  Phase 3: peer crates",
            "    /// (services, future transport / raft glue) reach the",
            "    /// kernel's in-tree Rust API surface",
            "    /// (`register_native_hook`, `prepare_audit_stream`,",
            "    /// `sys_*` direct) through this accessor instead of the",
            "    /// `pub(crate) inner` field, which other crates can't see.",
            "    pub fn kernel_ref(&self) -> &Kernel {",
            "        &self.inner",
            "    }",
            "",
            "    /// Clone the inner `Arc<Kernel>`.  Same Phase 3 motivation",
            "    /// as [`Self::kernel_ref`] but returns an owned `Arc<Kernel>`",
            "    /// for callers that need to spawn tasks holding the kernel",
            "    /// (e.g. `transport::grpc::start_vfs_grpc_server` clones the",
            "    /// Arc into a tonic worker task).",
            "    pub fn kernel_arc(&self) -> Arc<Kernel> {",
            "        Arc::clone(&self.inner)",
            "    }",
            "}",
            "",
            "#[pymethods]",
            "impl PyKernel {",
            "    // ── Constructor ────────────────────────────────────────────────────",
            "",
            "    #[new]",
            "    fn new() -> Self {",
            "        let inner = Arc::new(Kernel::new());",
            "        let write_buffer_flusher =",
            "            Kernel::spawn_write_buffer_flusher(&inner, std::time::Duration::from_millis(250));",
            "        Self {",
            "            inner,",
            "            _write_buffer_flusher: write_buffer_flusher,",
            "            hooks: RwLock::new(HookRegistry::new()),",
            "        }",
            "    }",
            "",
            "    // ── Lock Manager wiring ──────────────────────────────────────────",
            "",
            "    fn set_vfs_lock_timeout(&self, timeout_ms: u64) {",
            "        self.inner.set_vfs_lock_timeout(timeout_ms);",
            "    }",
            "",
            "    /// Set node advertise address for origin-aware metadata.",
            "    fn set_self_address(&self, addr: &str) {",
            "        self.inner.set_self_address(addr);",
            "    }",
            "",
            "    // ── §13 Permission gate wiring ──────────────────────────────────",
            "",
            "    /// Register a Python permission provider (wraps in PyPermissionProviderAdapter).",
            "    fn set_permission_provider(&self, provider: Py<PyAny>) {",
            "        let adapter = PyPermissionProviderAdapter { checker: provider };",
            "        self.inner",
            "            .set_permission_provider(std::sync::Arc::new(adapter));",
            "    }",
            "",
            "    /// Configure admin bypass (default: true).",
            "    fn set_permission_admin_bypass(&self, enabled: bool) {",
            "        self.inner.set_permission_admin_bypass(enabled);",
            "    }",
            "",
            "    /// Invalidate permission lease for a specific path.",
            "    fn permission_lease_invalidate_path(&self, path: &str) {",
            "        self.inner.permission_lease_invalidate_path(path);",
            "    }",
            "",
            "    /// Invalidate permission leases for a specific agent.",
            "    fn permission_lease_invalidate_agent(&self, agent_id: &str) {",
            "        self.inner.permission_lease_invalidate_agent(agent_id);",
            "    }",
            "",
            "    /// Invalidate all permission leases.",
            "    fn permission_lease_invalidate_all(&self) {",
            "        self.inner.permission_lease_invalidate_all();",
            "    }",
            "",
            "    // ── MetaStore wiring ──────────────────────────────────────────────",
            "",
            "    /// Wire LocalMetaStore by path — Rust kernel opens redb directly.",
            "    /// Eliminates GIL crossing on every metastore.get/put.",
            "    fn set_metastore_path(&self, path: &str) -> PyResult<()> {",
            "        self.inner.set_metastore_path(path).map_err(Into::into)",
            "    }",
            "",
            "    /// Drop global + per-mount redb metastores so a subsequent kernel",
            "    /// can reopen the same redb path without ``Database already open``.",
            "    /// Called by Python ``NexusFS.close`` / nested ``ephemeral_mount``.",
            "    fn release_metastores(&self) {",
            "        self.inner.release_metastores()",
            "    }",
            "",
            "    // ── MetaStore proxy methods (for RustMetastoreProxy) ──────────────",
            "",
            "    fn metastore_get(&self, py: Python<'_>, path: &str) -> PyResult<Option<Py<PyAny>>> {",
            "        match self",
            "            .inner",
            "            .metastore_get(path)",
            "            .map_err::<PyErr, _>(Into::into)?",
            "        {",
            "            Some(meta) => {",
            "                let obj = to_python_metadata(py, &meta)",
            '                    .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("{e:?}")))?;',
            "                Ok(Some(obj.into()))",
            "            }",
            "            None => Ok(None),",
            "        }",
            "    }",
            "",
            "    fn metastore_put(&self, py: Python<'_>, metadata: &Bound<'_, PyAny>) -> PyResult<()> {",
            "        let meta = extract_metadata(py, metadata)",
            '            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("{e:?}")))?;',
            "        self.inner",
            "            .metastore_put(&meta.path.clone(), meta)",
            "            .map_err(Into::into)",
            "    }",
            "",
            "    fn metastore_delete(&self, path: &str) -> PyResult<bool> {",
            "        self.inner.metastore_delete(path).map_err(Into::into)",
            "    }",
            "",
            "    fn metastore_get_batch(",
            "        &self,",
            "        py: Python<'_>,",
            "        paths: Vec<String>,",
            "    ) -> PyResult<Vec<Option<Py<PyAny>>>> {",
            "        let items = self",
            "            .inner",
            "            .metastore_get_batch(&paths)",
            "            .map_err::<PyErr, _>(Into::into)?;",
            "        let mut result = Vec::with_capacity(items.len());",
            "        for opt in &items {",
            "            match opt {",
            "                Some(meta) => result.push(Some(",
            "                    to_python_metadata(py, meta)",
            '                        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("{e:?}")))?',
            "                        .into(),",
            "                )),",
            "                None => result.push(None),",
            "            }",
            "        }",
            "        Ok(result)",
            "    }",
            "",
            "    #[pyo3(signature = (prefix, recursive=true, limit=1000, cursor=None))]",
            "    fn metastore_list_paginated(",
            "        &self,",
            "        py: Python<'_>,",
            "        prefix: &str,",
            "        recursive: bool,",
            "        limit: usize,",
            "        cursor: Option<&str>,",
            "    ) -> PyResult<Py<PyAny>> {",
            "        let page = self",
            "            .inner",
            "            .metastore_list_paginated(prefix, recursive, limit, cursor)",
            "            .map_err::<PyErr, _>(Into::into)?;",
            "        let dict = PyDict::new(py);",
            "        let items: Vec<Py<PyAny>> = page",
            "            .items",
            "            .iter()",
            "            .map(|m| {",
            "                to_python_metadata(py, m)",
            '                    .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("{e:?}")))',
            "                    .map(Into::into)",
            "            })",
            "            .collect::<PyResult<Vec<_>>>()?;",
            '        dict.set_item("items", items)?;',
            '        dict.set_item("next_cursor", page.next_cursor)?;',
            '        dict.set_item("has_more", page.has_more)?;',
            '        dict.set_item("total_count", page.total_count)?;',
            "        Ok(dict.into())",
            "    }",
            "",
            "    // ── Tier 2 convenience methods ────────────────────────────────────",
            "",
            '    #[pyo3(signature = (paths, zone_id="root"))]',
            "    fn stat_batch(&self, py: Python<'_>, paths: Vec<String>, zone_id: &str) -> Vec<Option<Py<PyAny>>> {",
            "        use crate::kernel::convenience::KernelConvenience;",
            "        let results = self.inner.stat_batch(&paths, zone_id);",
            "        results",
            "            .into_iter()",
            "            .map(|opt| opt.map(|stat| stat_result_to_pydict(py, &stat).into()))",
            "            .collect()",
            "    }",
            "",
            "    fn set_xattr(&self, path: &str, key: &str, value: String) -> PyResult<()> {",
            "        use crate::kernel::convenience::KernelConvenience;",
            "        self.inner",
            "            .set_xattr(path, key, value, contracts::ROOT_ZONE_ID)",
            "            .map_err(Into::into)",
            "    }",
            "",
            "    fn get_xattr(&self, path: &str, key: &str) -> PyResult<Option<String>> {",
            "        use crate::kernel::convenience::KernelConvenience;",
            "        self.inner",
            "            .get_xattr(path, key, contracts::ROOT_ZONE_ID)",
            "            .map_err(Into::into)",
            "    }",
            "",
            "    fn get_xattr_bulk(&self, py: Python<'_>, paths: Vec<String>, key: &str) -> PyResult<Py<PyAny>> {",
            "        use crate::kernel::convenience::KernelConvenience;",
            "        let pairs = self",
            "            .inner",
            "            .get_xattr_bulk(&paths, key, contracts::ROOT_ZONE_ID)",
            "            .map_err::<PyErr, _>(Into::into)?;",
            "        let dict = PyDict::new(py);",
            "        for (path, value) in pairs {",
            "            match value {",
            "                Some(text) => dict.set_item(path, text)?,",
            "                None => dict.set_item(path, py.None())?,",
            "            }",
            "        }",
            "        Ok(dict.into())",
            "    }",
            "",
            '    #[pyo3(signature = (parent_path, zone_id="root", is_admin=false, limit=0, cursor=None))]',
            "    fn readdir_paged(&self, py: Python<'_>, parent_path: &str, zone_id: &str, is_admin: bool, limit: usize, cursor: Option<&str>) -> Py<PyAny> {",
            "        let result = self.inner.readdir_paged(parent_path, zone_id, is_admin, limit, cursor);",
            "        let dict = PyDict::new(py);",
            '        let _ = dict.set_item("items", &result.items);',
            '        let _ = dict.set_item("next_cursor", &result.next_cursor);',
            '        let _ = dict.set_item("has_more", result.has_more);',
            "        dict.into()",
            "    }",
            "",
            "    // ── Advisory lock syscalls (F4 C5) ──────────────────────────────",
            "",
            '    #[pyo3(signature = (path, lock_id="", mode="exclusive", max_holders=1, ttl_secs=60, holder_info=""))]',
            "    #[allow(clippy::too_many_arguments)]",
            "    fn sys_lock(",
            "        &self,",
            "        path: &str,",
            "        lock_id: &str,",
            "        mode: &str,",
            "        max_holders: u32,",
            "        ttl_secs: u64,",
            "        holder_info: &str,",
            "    ) -> PyResult<Option<String>> {",
            "        let parsed_mode = match mode.to_ascii_lowercase().as_str() {",
            '            "exclusive" => crate::lock_manager::KernelLockMode::Exclusive,',
            '            "shared" => crate::lock_manager::KernelLockMode::Shared,',
            "            other => {",
            "                return Err(pyo3::exceptions::PyValueError::new_err(format!(",
            "                    \"invalid lock mode '{}': expected 'exclusive' or 'shared'\",",
            "                    other",
            "                )));",
            "            }",
            "        };",
            "        self.inner",
            "            .sys_lock(path, lock_id, parsed_mode, max_holders, ttl_secs, holder_info)",
            "            .map_err(Into::into)",
            "    }",
            "",
            '    #[pyo3(signature = (path, lock_id="", force=false))]',
            "    fn sys_unlock(&self, path: &str, lock_id: &str, force: bool) -> PyResult<bool> {",
            "        self.inner",
            "            .sys_unlock(path, lock_id, force)",
            "            .map_err(Into::into)",
            "    }",
            "",
            "    /// Linearizable enumeration of locks under `prefix`, capped",
            "    /// at `limit`. Returns a list of dicts shaped exactly like",
            "    /// the old Python `_format_lock_info` output:",
            '    ///   { "path": str, "mode": "mutex" | "semaphore",',
            '    ///     "max_holders": int,',
            '    ///     "holders": [ { "lock_id": str, "holder_info": str,',
            '    ///                     "acquired_at": int,',
            '    ///                     "expires_at": int } ] }',
            "    ///",
            "    /// Internal metastore-proxy helper, NOT a separate syscall.",
            "    /// Called by the Python `NexusFS.sys_readdir` intercept for",
            "    /// `/__sys__/locks/` (Linux /proc/locks convention), which",
            "    /// preserves the existing user-facing `sys_readdir` API.",
            "    /// The per-holder `LockMode` tracked inside the state",
            "    /// machine for RW conflict correctness is intentionally",
            "    /// NOT surfaced here — the dict shape matches Python's",
            "    /// pre-F4 `_format_lock_info` contract so the surviving",
            "    /// `/api/v2/locks` + `nx locks list` consumers keep",
            "    /// parsing unchanged.",
            '    #[pyo3(signature = (prefix="", limit=1024))]',
            "    fn metastore_list_locks<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        prefix: &str,",
            "        limit: usize,",
            "    ) -> PyResult<Vec<Bound<'py, PyDict>>> {",
            "        let locks = self",
            "            .inner",
            "            .metastore_list_locks(prefix, limit)",
            "            .map_err::<PyErr, _>(Into::into)?;",
            "        let mut out = Vec::with_capacity(locks.len());",
            "        for lock in &locks {",
            "            let dict = PyDict::new(py);",
            '            dict.set_item("path", &lock.path)?;',
            "            // Lock-level mode is the computed display label,",
            "            // not the stored per-holder conflict mode.",
            '            let label = if lock.max_holders == 1 { "mutex" } else { "semaphore" };',
            '            dict.set_item("mode", label)?;',
            '            dict.set_item("max_holders", lock.max_holders)?;',
            "            let holders = pyo3::types::PyList::empty(py);",
            "            for h in &lock.holders {",
            "                let h_dict = PyDict::new(py);",
            '                h_dict.set_item("lock_id", &h.lock_id)?;',
            '                h_dict.set_item("holder_info", &h.holder_info)?;',
            '                h_dict.set_item("acquired_at", h.acquired_at_secs)?;',
            '                h_dict.set_item("expires_at", h.expires_at_secs)?;',
            "                holders.append(h_dict)?;",
            "            }",
            '            dict.set_item("holders", holders)?;',
            "            out.push(dict);",
            "        }",
            "        Ok(out)",
            "    }",
            "",
            "    // ── R10c: direct CAS surface — PyKernel delegators ───────────────",
            "    //",
            "    // Thin wrappers around Kernel::cas_* that release the GIL for the",
            "    // storage work. Each method mirrors a Python CASAddressingEngine",
            "    // hot-path call so the Python delegator can collapse to `return",
            "    // self._kernel.cas_*(...)`. Error conversion reuses the KernelError",
            "    // → PyErr pipeline so NotFound surfaces as NexusFileNotFoundError",
            "    // and I/O surfaces as BackendError, both with mount + op breadcrumbs.",
            "",
            "    #[pyo3(signature = (mount_point, zone_id, content, *, ttl_seconds=None))]",
            "    fn cas_write<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        mount_point: &str,",
            "        zone_id: &str,",
            "        content: Vec<u8>,",
            "        ttl_seconds: Option<u64>,",
            "    ) -> PyResult<(String, bool)> {",
            "        py.detach(|| self.inner.cas_write(mount_point, zone_id, &content, ttl_seconds))",
            "            .map_err(Into::into)",
            "    }",
            "",
            "    #[pyo3(signature = (mount_point, zone_id, content_id, *, origins=None))]",
            "    fn cas_read<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        mount_point: &str,",
            "        zone_id: &str,",
            "        content_id: &str,",
            "        origins: Option<Vec<String>>,",
            "    ) -> PyResult<Py<PyBytes>> {",
            "        let origins_vec = origins.unwrap_or_default();",
            "        let bytes = py",
            "            .detach(|| self.inner.cas_read(mount_point, zone_id, content_id, &origins_vec))",
            "            .map_err::<PyErr, _>(Into::into)?;",
            "        Ok(PyBytes::new(py, &bytes).unbind())",
            "    }",
            "",
            "    #[pyo3(signature = (mount_point, zone_id, content_id, start, end, *, origins=None))]",
            "    #[allow(clippy::too_many_arguments)]",
            "    fn cas_read_range<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        mount_point: &str,",
            "        zone_id: &str,",
            "        content_id: &str,",
            "        start: u64,",
            "        end: u64,",
            "        origins: Option<Vec<String>>,",
            "    ) -> PyResult<Py<PyBytes>> {",
            "        let origins_vec = origins.unwrap_or_default();",
            "        let bytes = py",
            "            .detach(|| {",
            "                self.inner.cas_read_range(",
            "                    mount_point,",
            "                    zone_id,",
            "                    content_id,",
            "                    start,",
            "                    end,",
            "                    &origins_vec,",
            "                )",
            "            })",
            "            .map_err::<PyErr, _>(Into::into)?;",
            "        Ok(PyBytes::new(py, &bytes).unbind())",
            "    }",
            "",
            "    fn cas_delete<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        mount_point: &str,",
            "        zone_id: &str,",
            "        content_id: &str,",
            "    ) -> PyResult<()> {",
            "        py.detach(|| self.inner.cas_delete(mount_point, zone_id, content_id))",
            "            .map_err(Into::into)",
            "    }",
            "",
            "    fn cas_exists<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        mount_point: &str,",
            "        zone_id: &str,",
            "        content_id: &str,",
            "    ) -> PyResult<bool> {",
            "        py.detach(|| self.inner.cas_exists(mount_point, zone_id, content_id))",
            "            .map_err(Into::into)",
            "    }",
            "",
            "    fn cas_size<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        mount_point: &str,",
            "        zone_id: &str,",
            "        content_id: &str,",
            "    ) -> PyResult<u64> {",
            "        py.detach(|| self.inner.cas_size(mount_point, zone_id, content_id))",
            "            .map_err(Into::into)",
            "    }",
            "",
            "    fn cas_is_chunked<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        mount_point: &str,",
            "        zone_id: &str,",
            "        content_id: &str,",
            "    ) -> PyResult<bool> {",
            "        py.detach(|| self.inner.cas_is_chunked(mount_point, zone_id, content_id))",
            "            .map_err(Into::into)",
            "    }",
            "",
            "    #[pyo3(signature = (mount_point, zone_id, old_hash, buf, offset, *, origins=None))]",
            "    #[allow(clippy::too_many_arguments)]",
            "    fn cas_write_partial<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        mount_point: &str,",
            "        zone_id: &str,",
            "        old_hash: &str,",
            "        buf: Vec<u8>,",
            "        offset: u64,",
            "        origins: Option<Vec<String>>,",
            "    ) -> PyResult<String> {",
            "        let origins_vec = origins.unwrap_or_default();",
            "        py.detach(|| {",
            "            self.inner.cas_write_partial(",
            "                mount_point,",
            "                zone_id,",
            "                old_hash,",
            "                &buf,",
            "                offset,",
            "                &origins_vec,",
            "            )",
            "        })",
            "        .map_err(Into::into)",
            "    }",
            "",
            "    // ── R10d: LLM streaming entry point (OpenAI / SSE) ───────────────",
            "    //",
            "    // Resolves (mount_point, zone_id) → OpenAIBackend via",
            "    // `ObjectStore::as_llm_streaming()`, then runs the full SSE → DT_STREAM",
            "    // → CAS-persist pipeline in a GIL-free worker. Caller is expected to",
            "    // invoke via `asyncio.to_thread(...)` so the event loop is not blocked",
            "    // for the duration of the completion.",
            "",
            '    #[cfg(feature = "connectors")]',
            "    fn llm_start_streaming<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        mount_point: &str,",
            "        zone_id: &str,",
            "        request_bytes: Vec<u8>,",
            "        stream_path: &str,",
            "    ) -> PyResult<()> {",
            "        let canonical = crate::vfs_router::canonicalize_mount_path(mount_point, zone_id);",
            "        let entry = self",
            "            .inner",
            "            .vfs_router",
            "            .get_canonical(&canonical)",
            "            .ok_or_else(|| {",
            "                pyo3::exceptions::PyFileNotFoundError::new_err(format!(",
            '                    "llm_start_streaming: mount not found: {}@{}",',
            "                    mount_point, zone_id",
            "                ))",
            "            })?;",
            "        let backend = entry",
            "            .backend",
            "            .as_ref()",
            "            .ok_or_else(|| {",
            "                pyo3::exceptions::PyRuntimeError::new_err(format!(",
            '                    "llm_start_streaming: mount has no backend at {}",',
            "                    mount_point",
            "                ))",
            "            })?;",
            "        let llm = backend.as_llm_streaming().ok_or_else(|| {",
            "            pyo3::exceptions::PyRuntimeError::new_err(format!(",
            '                "llm_start_streaming: backend at mount {} does not support streaming",',
            "                mount_point",
            "            ))",
            "        })?;",
            "        let stream_manager = Arc::clone(&self.inner.stream_manager);",
            "        let stream_path_owned = stream_path.to_string();",
            "        py.detach(move || {",
            "            // Create the DT_STREAM buffer before run_streaming writes to it.",
            "            // 64 KiB matches the default capacity used in unit tests.",
            "            let _ = stream_manager.create(&stream_path_owned, 64 * 1024);",
            "            llm.run_streaming(&request_bytes, &stream_path_owned, &stream_manager)",
            "                .map_err(|e| {",
            "                    pyo3::exceptions::PyRuntimeError::new_err(format!(",
            '                        "llm_start_streaming: {}",',
            "                        e",
            "                    ))",
            "                })",
            "        })",
            "    }",
            "",
            "    // ── sys_setattr — unified mount/attr syscall ─────────────────────",
            "",
            '    #[pyo3(signature = (path, entry_type, backend_name="", local_root=None, fsync=false, backend_type="", follow_symlinks=true, openai_base_url=None, openai_api_key=None, openai_model=None, openai_blob_root=None, anthropic_base_url=None, anthropic_api_key=None, anthropic_model=None, anthropic_blob_root=None, s3_bucket=None, s3_prefix=None, aws_region=None, aws_access_key=None, aws_secret_key=None, s3_endpoint=None, gcs_bucket=None, gcs_prefix=None, access_token=None, root_folder_id=None, bot_token=None, default_channel=None, hn_stories_per_feed=None, hn_include_comments=None, cli_command=None, cli_service=None, cli_auth_env_json=None, x_bearer_token=None, metastore_path=None, io_profile="balanced", zone_id="root", is_external=false, capacity=65536, mime_type=None, modified_at_ms=None, content_id=None, size=None, version=None, created_at_ms=None, read_fd=None, write_fd=None, server_address=None, remote_auth_token=None, remote_ca_pem=None, remote_cert_pem=None, remote_key_pem=None, remote_timeout=30.0, link_target=None, source=None))]',
            "    #[allow(clippy::too_many_arguments)]",
            "    fn sys_setattr<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        path: &str,",
            "        entry_type: i32,",
            "        backend_name: &str,",
            "        local_root: Option<&str>,",
            "        fsync: bool,",
            "        backend_type: &str,",
            "        follow_symlinks: bool,",
            "        openai_base_url: Option<&str>,",
            "        openai_api_key: Option<&str>,",
            "        openai_model: Option<&str>,",
            "        openai_blob_root: Option<&str>,",
            "        anthropic_base_url: Option<&str>,",
            "        anthropic_api_key: Option<&str>,",
            "        anthropic_model: Option<&str>,",
            "        anthropic_blob_root: Option<&str>,",
            "        s3_bucket: Option<&str>,",
            "        s3_prefix: Option<&str>,",
            "        aws_region: Option<&str>,",
            "        aws_access_key: Option<&str>,",
            "        aws_secret_key: Option<&str>,",
            "        s3_endpoint: Option<&str>,",
            "        gcs_bucket: Option<&str>,",
            "        gcs_prefix: Option<&str>,",
            "        access_token: Option<&str>,",
            "        root_folder_id: Option<&str>,",
            "        bot_token: Option<&str>,",
            "        default_channel: Option<&str>,",
            "        hn_stories_per_feed: Option<usize>,",
            "        hn_include_comments: Option<bool>,",
            "        cli_command: Option<&str>,",
            "        cli_service: Option<&str>,",
            "        cli_auth_env_json: Option<&str>,",
            "        x_bearer_token: Option<&str>,",
            "        metastore_path: Option<&str>,",
            "        io_profile: &str,",
            "        zone_id: &str,",
            "        is_external: bool,",
            "        capacity: usize,",
            "        mime_type: Option<&str>,",
            "        modified_at_ms: Option<i64>,",
            "        content_id: Option<&str>,",
            "        size: Option<u64>,",
            "        version: Option<u32>,",
            "        created_at_ms: Option<i64>,",
            "        read_fd: Option<i32>,",
            "        write_fd: Option<i32>,",
            "        server_address: Option<&str>,",
            "        remote_auth_token: Option<&str>,",
            "        remote_ca_pem: Option<&[u8]>,",
            "        remote_cert_pem: Option<&[u8]>,",
            "        remote_key_pem: Option<&[u8]>,",
            "        remote_timeout: f64,",
            "        link_target: Option<&str>,",
            "        source: Option<&str>,",
            "    ) -> PyResult<Py<PyAny>> {",
            "        // 17-way backend-type construction lives in",
            "        // `backends::python::factory::DefaultObjectStoreProvider`.",
            "        // Kernel reaches concrete backend types through the §3.B.2",
            "        // `ObjectStoreProvider` trait, installed by `nexus-cdylib`",
            "        // at module init.",
            "        let provider = crate::hal::object_store_provider::get_provider()",
            "            .ok_or_else(|| {",
            "                pyo3::exceptions::PyRuntimeError::new_err(",
            '                    "sys_setattr: ObjectStoreProvider not registered — non-cdylib build needs to call kernel::hal::object_store_provider::set_provider at boot",',
            "                )",
            "            })?;",
            "        // peer_client is a `RwLock<Arc<dyn PeerBlobClient>>` so the",
            "        // cdylib's `install_transport_wiring` swaps the Noop default",
            "        // for the real concrete impl post-boot. Lock and clone the",
            "        // inner `Arc` for the provider call so the read guard does",
            "        // not outlive this scope.",
            "        let peer_client_arc: Arc<dyn crate::hal::peer::PeerBlobClient> =",
            "            Arc::clone(&self.inner.peer_client.read());",
            "        // Snapshot of self_address — `cas_local` plumbs into the per-mount",
            "        // `GrpcChunkFetcher` so chunk-miss scatter skips this node.  Snapshotted",
            "        // once here so the provider call sees a consistent `&str` borrow.",
            "        let self_addr_snapshot: Option<String> = self.inner.self_address_string();",
            "        let provider_args = crate::hal::object_store_provider::ObjectStoreProviderArgs {",
            "            backend_type, backend_name,",
            "            local_root, fsync, follow_symlinks,",
            "            openai_base_url, openai_api_key, openai_model, openai_blob_root,",
            "            anthropic_base_url, anthropic_api_key, anthropic_model, anthropic_blob_root,",
            "            s3_bucket, s3_prefix, aws_region, aws_access_key, aws_secret_key, s3_endpoint,",
            "            gcs_bucket, gcs_prefix,",
            "            access_token, root_folder_id, bot_token, default_channel,",
            "            hn_stories_per_feed, hn_include_comments,",
            "            cli_command, cli_service, cli_auth_env_json,",
            "            x_bearer_token,",
            "            server_address, remote_auth_token,",
            "            remote_ca_pem, remote_cert_pem, remote_key_pem, remote_timeout,",
            "            peer_client: &peer_client_arc,",
            "            self_address: self_addr_snapshot.as_deref(),",
            "            runtime: self.inner.runtime(),",
            "        };",
            "        let backend_result = provider.build(&provider_args)",
            "            .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;",
            "        let backend = backend_result.backend;",
            "        let remote_metastore = backend_result.pending_remote_meta_store;",
            "",
            "        // MetaStore resolution: metastore_path -> LocalMetaStore.",
            "        // Federation DT_MOUNT auto-resolves its raft backing via",
            "        // `resolve_federation_mount_backing` inside Kernel::sys_setattr",
            "        // (R20.18.3), so Python always passes `None` for raft_backend.",
            "        let metastore: Option<Arc<dyn crate::meta_store::MetaStore>> =",
            "            if let Some(ms_path) = metastore_path {",
            "                let ms = crate::meta_store::LocalMetaStore::open(",
            "                    std::path::Path::new(ms_path),",
            "                )",
            '                .map_err(|e| pyo3::exceptions::PyIOError::new_err(format!("LocalMetaStore: {e:?}")))?;',
            "                Some(Arc::new(ms) as Arc<dyn crate::meta_store::MetaStore>)",
            "            } else {",
            "                None",
            "            };",
            "",
            "        let result = self.inner",
            "            .sys_setattr(",
            "                path,",
            "                entry_type,",
            "                backend_name,",
            "                backend,",
            "                metastore,",
            "                None, // raft_backend — auto-resolved inside Kernel::sys_setattr",
            "                io_profile,",
            "                zone_id,",
            "                is_external,",
            "                capacity,",
            "                read_fd,",
            "                write_fd,",
            "                mime_type,",
            "                modified_at_ms,",
            "                content_id,",
            "                size,",
            "                version,",
            "                created_at_ms,",
            "                link_target,",
            "                source,",
            "                remote_metastore,",
            "            )",
            "            .map_err::<PyErr, _>(Into::into)?;",
            "",
            "        let dict = PyDict::new(py);",
            '        dict.set_item("path", result.path)?;',
            '        dict.set_item("created", result.created)?;',
            '        dict.set_item("entry_type", result.entry_type)?;',
            "        if let Some(bn) = result.backend_name {",
            '            dict.set_item("backend_name", bn)?;',
            "        }",
            "        if let Some(cap) = result.capacity {",
            '            dict.set_item("capacity", cap)?;',
            "        }",
            "        if !result.updated.is_empty() {",
            '            dict.set_item("updated", result.updated)?;',
            "        }",
            "        if let Some(shm) = result.shm_path {",
            '            dict.set_item("shm_path", shm)?;',
            "        }",
            "        if let Some(fd) = result.data_rd_fd {",
            '            dict.set_item("data_rd_fd", fd)?;',
            "        }",
            "        if let Some(fd) = result.space_rd_fd {",
            '            dict.set_item("space_rd_fd", fd)?;',
            "        }",
            "        Ok(dict.into())",
            "    }",
            "",
            "    // ── Router proxy methods ───────────────────────────────────────────",
            "",
            "    fn has_mount(&self, mount_point: &str, zone_id: &str) -> bool {",
            "        self.inner.has_mount(mount_point, zone_id)",
            "    }",
            "",
            "    fn get_mount_points(&self) -> Vec<String> {",
            "        self.inner.get_mount_points()",
            "    }",
            "",
            '    #[pyo3(signature = (mount_point, zone_id="root"))]',
            "    fn kernel_unmount(&self, mount_point: &str, zone_id: &str) -> bool {",
            "        self.inner.dlc.unmount(&self.inner, mount_point, zone_id)",
            "    }",
            "",
            "    // ── IPC Registry — Pipe methods ──────────────────────────────────",
            "",
            "    fn create_pipe(&self, path: &str, capacity: usize) -> PyResult<()> {",
            "        self.inner.create_pipe(path, capacity).map_err(Into::into)",
            "    }",
            "",
            "    fn destroy_pipe(&self, path: &str) -> PyResult<()> {",
            "        self.inner.destroy_pipe(path).map_err(Into::into)",
            "    }",
            "",
            "    fn close_pipe(&self, path: &str) -> PyResult<()> {",
            "        self.inner.close_pipe(path).map_err(Into::into)",
            "    }",
            "",
            "    fn has_pipe(&self, path: &str) -> bool {",
            "        self.inner.has_pipe(path)",
            "    }",
            "",
            "    fn pipe_write_nowait(&self, path: &str, data: &[u8]) -> PyResult<usize> {",
            "        self.inner.pipe_write_nowait(path, data).map_err(Into::into)",
            "    }",
            "",
            "    fn pipe_read_nowait<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Option<Bound<'py, PyBytes>>> {",
            "        match self.inner.pipe_read_nowait(path) {",
            "            Ok(Some(data)) => Ok(Some(PyBytes::new(py, &data))),",
            "            Ok(None) => Ok(None),",
            "            Err(e) => Err(e.into()),",
            "        }",
            "    }",
            "",
            "    fn list_pipes(&self) -> Vec<String> {",
            "        self.inner.list_pipes()",
            "    }",
            "",
            "    fn close_all_pipes(&self) {",
            "        self.inner.close_all_pipes()",
            "    }",
            "",
            "    // ── IPC Registry — Stream methods ────────────────────────────────",
            "",
            "    fn create_stream(&self, path: &str, capacity: usize) -> PyResult<()> {",
            "        self.inner.create_stream(path, capacity).map_err(Into::into)",
            "    }",
            "",
            "    fn destroy_stream(&self, path: &str) -> PyResult<()> {",
            "        self.inner.destroy_stream(path).map_err(Into::into)",
            "    }",
            "",
            "    fn close_stream(&self, path: &str) -> PyResult<()> {",
            "        self.inner.close_stream(path).map_err(Into::into)",
            "    }",
            "",
            "    fn has_stream(&self, path: &str) -> bool {",
            "        self.inner.has_stream(path)",
            "    }",
            "",
            "    fn stream_write_nowait(&self, path: &str, data: &[u8]) -> PyResult<usize> {",
            "        self.inner.stream_write_nowait(path, data).map_err(Into::into)",
            "    }",
            "",
            "    fn stream_read_at<'py>(&self, py: Python<'py>, path: &str, offset: usize) -> PyResult<Option<(Bound<'py, PyBytes>, usize)>> {",
            "        match self.inner.stream_read_at(path, offset) {",
            "            Ok(Some((data, next))) => Ok(Some((PyBytes::new(py, &data), next))),",
            "            Ok(None) => Ok(None),",
            "            Err(e) => Err(e.into()),",
            "        }",
            "    }",
            "",
            "    fn stream_read_batch<'py>(&self, py: Python<'py>, path: &str, offset: usize, count: usize) -> PyResult<(Vec<Bound<'py, PyBytes>>, usize)> {",
            "        let (msgs, next) = self.inner.stream_read_batch(path, offset, count).map_err(|e| -> PyErr { e.into() })?;",
            "        let py_msgs: Vec<Bound<'py, PyBytes>> = msgs.iter().map(|m| PyBytes::new(py, m)).collect();",
            "        Ok((py_msgs, next))",
            "    }",
            "",
            "    fn stream_collect_all<'py>(&self, py: Python<'py>, path: &str) -> PyResult<Bound<'py, PyBytes>> {",
            "        let data = self.inner.stream_collect_all(path).map_err(|e| -> PyErr { e.into() })?;",
            "        Ok(PyBytes::new(py, &data))",
            "    }",
            "",
            "    fn list_streams(&self) -> Vec<String> {",
            "        self.inner.list_streams()",
            "    }",
            "",
            "    fn close_all_streams(&self) {",
            "        self.inner.close_all_streams()",
            "    }",
            "",
            "    // ── Trie proxy methods ─────────────────────────────────────────────",
            "",
            "    fn trie_register(&self, pattern: &str, resolver_idx: usize) -> PyResult<()> {",
            "        self.inner",
            "            .trie_register(pattern, resolver_idx)",
            "            .map_err(Into::into)",
            "    }",
            "",
            "    fn trie_unregister(&self, resolver_idx: usize) -> bool {",
            "        self.inner.trie_unregister(resolver_idx)",
            "    }",
            "",
            "    fn trie_lookup(&self, path: &str) -> Option<usize> {",
            "        self.inner.trie_lookup(path)",
            "    }",
            "",
            "    fn trie_len(&self) -> usize {",
            "        self.inner.trie_len()",
            "    }",
            "",
            "    // ── Hook proxy methods ─────────────────────────────────────────────",
            "",
            "    fn register_hook(&self, py: Python<'_>, op: &str, hook: Py<PyAny>) -> PyResult<()> {",
            "        let hook_ref = hook.bind(py);",
            "        let name: String = hook_ref",
            '            .getattr("name")',
            "            .and_then(|n| n.extract())",
            '            .unwrap_or_else(|_| "<hook>".to_string());',
            "",
            '        let pre_attr = format!("on_pre_{op}");',
            "        let has_pre = hook_ref",
            "            .getattr(pre_attr.as_str())",
            "            .map(|attr| !attr.is_none())",
            "            .unwrap_or(false);",
            "",
            '        let post_attr = format!("on_post_{op}");',
            "        let is_async_post = match hook_ref.getattr(post_attr.as_str()) {",
            "            Ok(post_fn) => py",
            '                .import("inspect")?',
            '                .call_method1("iscoroutinefunction", (post_fn,))?',
            "                .extract::<bool>()",
            "                .unwrap_or(false),",
            "            Err(_) => false,",
            "        };",
            "",
            '        #[cfg(feature = "py-hook-adapters")]',
            "        {",
            "            let adapter = PyInterceptHookAdapter::new(py, hook.clone_ref(py));",
            "            self.hooks",
            "                .write()",
            "                .register(op, Box::new(adapter), hook, has_pre, is_async_post, name);",
            "            Ok(())",
            "        }",
            "",
            '        #[cfg(not(feature = "py-hook-adapters"))]',
            "        {",
            "            let _ = (hook, has_pre, is_async_post, name);",
            "            Err(pyo3::exceptions::PyRuntimeError::new_err(",
            "                \"register_hook requires feature 'py-hook-adapters'\",",
            "            ))",
            "        }",
            "    }",
            "",
            "    fn unregister_hook(&self, py: Python<'_>, op: &str, hook: &Bound<'_, PyAny>) -> bool {",
            "        self.hooks.write().unregister(py, op, hook)",
            "    }",
            "",
            "    fn get_pre_hooks(&self, py: Python<'_>, op: &str) -> Vec<Py<PyAny>> {",
            "        self.hooks.read_unconditional().get_pre_hooks(py, op)",
            "    }",
            "",
            "    fn get_post_hooks(&self, py: Python<'_>, op: &str) -> (Vec<Py<PyAny>>, Vec<Py<PyAny>>) {",
            "        self.hooks.read_unconditional().get_post_hooks(py, op)",
            "    }",
            "",
            "    fn get_all_hooks(&self, py: Python<'_>, op: &str) -> Vec<Py<PyAny>> {",
            "        self.hooks.read_unconditional().get_all_hooks(py, op)",
            "    }",
            "",
            "    fn hook_count(&self, op: &str) -> usize {",
            "        self.hooks.read_unconditional().count(op)",
            "    }",
            "",
            "    // ── sys_watch (inotify equivalent) ───────────────────────────────",
            "",
            "    /// sys_watch — block until a matching file event or timeout.",
            "    /// Returns (event_type, path) tuple or None.",
            "    #[pyo3(signature = (pattern, timeout_ms))]",
            "    fn sys_watch(&self, py: Python<'_>, pattern: &str, timeout_ms: u64) -> Option<(String, String)> {",
            "        let event = py.detach(|| self.inner.sys_watch(pattern, timeout_ms));",
            "        event.map(|e| (e.event_type.as_str().to_string(), e.path.clone()))",
            "    }",
            "",
            "    // ── Observer dispatch (pure Rust) ────────────────────────────────",
            "",
            "    /// Dispatch event to all registered Rust-native observers (for DLC mount/unmount).",
            "    #[pyo3(signature = (event_type, path))]",
            "    fn dispatch_event(&self, event_type: &str, path: &str) -> PyResult<()> {",
            "        use crate::dispatch::FileEventType;",
            "        let etype = match event_type {",
            '            "file_write" => FileEventType::FileWrite,',
            '            "file_delete" => FileEventType::FileDelete,',
            '            "file_rename" => FileEventType::FileRename,',
            '            "metadata_change" => FileEventType::MetadataChange,',
            '            "dir_create" => FileEventType::DirCreate,',
            '            "dir_delete" => FileEventType::DirDelete,',
            '            "file_copy" => FileEventType::FileCopy,',
            '            "mount" => FileEventType::Mount,',
            '            "unmount" => FileEventType::Unmount,',
            "            other => return Err(pyo3::exceptions::PyValueError::new_err(",
            '                format!("unknown event type: {other}"),',
            "            )),",
            "        };",
            "        self.inner.dispatch_event(etype, path);",
            "        Ok(())",
            "    }",
            "",
            "    /// Flush pending Rust-native observer tasks (blocks until pool drains).",
            "    fn flush_observers(&self) {",
            "        self.inner.flush_observers();",
            "    }",
            "",
            "    /// Total observers (Rust-native + event buffers).",
            "    fn kernel_observer_count(&self) -> usize {",
            "        self.inner.observer_count()",
            "    }",
            "",
            "    // ── Hook counts ────────────────────────────────────────────────────",
            "",
            "    fn set_hook_count(&self, op: &str, count: u64) {",
            "        self.inner.set_hook_count(op, count);",
            "    }",
            "",
            "    // ── Public hook dispatch (for Tier 2 Python callers) ──────────────",
            "",
            "    /// Dispatch pre-hooks for an operation via Rust InterceptHook trait.",
            "    ///",
            "    /// Called by Tier 2 Python methods (read_range, stream, write, copy, etc.)",
            "    /// that build their own hook context objects. Tier 1 syscalls (sys_read,",
            "    /// sys_write, etc.) dispatch pre-hooks internally.",
            "    #[pyo3(signature = (op, hook_ctx))]",
            "    fn dispatch_pre_hooks(&self, op: &str, hook_ctx: Py<PyAny>) -> PyResult<()> {",
            "        if !self.inner.has_hooks(op) {",
            "            return Ok(());",
            "        }",
            "        let hooks = self.hooks.read_unconditional();",
            "        let impls = hooks.get_pre_hook_impls(op);",
            "        for hook in impls {",
            "            match op {",
            '                "read" => hook.on_pre_read(&hook_ctx)?,',
            '                "write" => hook.on_pre_write(&hook_ctx)?,',
            '                "delete" => hook.on_pre_delete(&hook_ctx)?,',
            '                "rename" => hook.on_pre_rename(&hook_ctx)?,',
            '                "mkdir" => hook.on_pre_mkdir(&hook_ctx)?,',
            '                "rmdir" => hook.on_pre_rmdir(&hook_ctx)?,',
            '                "copy" => hook.on_pre_copy(&hook_ctx)?,',
            '                "stat" => hook.on_pre_stat(&hook_ctx)?,',
            '                "access" => hook.on_pre_access(&hook_ctx)?,',
            '                "write_batch" => hook.on_pre_write_batch(&hook_ctx)?,',
            "                _ => {}",
            "            }",
            "        }",
            "        Ok(())",
            "    }",
            "",
            "    // ── Post-hook dispatch (sync Rust + return async for Python) ───────���",
            "",
            "    /// Dispatch post-hooks: sync in Rust, return async hooks for Python.",
            "    ///",
            "    /// Sync post-hooks: serial, fault-isolated (fire-and-forget).",
            "    /// Returns async hooks as Vec<Py<PyAny>> for Python asyncio.gather.",
            "    #[pyo3(signature = (op, hook_ctx))]",
            "    fn dispatch_post_hooks(",
            "        &self,",
            "        py: Python<'_>,",
            "        op: &str,",
            "        hook_ctx: Py<PyAny>,",
            "    ) -> PyResult<Vec<Py<PyAny>>> {",
            "        if !self.inner.has_hooks(op) {",
            "            return Ok(Vec::new());",
            "        }",
            "        // 1. Dispatch sync post-hooks in Rust (fire-and-forget)",
            "        self.dispatch_post_hooks_sync(op, &hook_ctx);",
            "",
            "        // 2. Return async hooks for Python to schedule",
            "        let hooks = self.hooks.read_unconditional();",
            "        let (_, async_hooks) = hooks.get_post_hooks(py, op);",
            "        Ok(async_hooks)",
            "    }",
            "",
            "    // ── Batch stat permission check (single FFI call for N paths) ──────",
            "",
            "    /// Batch stat permission check: dispatch stat pre-hooks for each path,",
            "    /// returning a bool per path (true = allowed, false = denied).",
            "    ///",
            "    /// Reduces N PyO3 boundary crossings to 1 for batch operations like",
            "    /// read_bulk and stat_bulk that check permissions on many paths.",
            "    #[pyo3(signature = (paths, ctx, permission))]",
            "    fn dispatch_pre_hooks_batch_stat(",
            "        &self,",
            "        py: Python<'_>,",
            "        paths: Vec<String>,",
            "        ctx: &PyOperationContext,",
            "        permission: &str,",
            "    ) -> PyResult<Vec<bool>> {",
            "        // Fast path: no stat hooks → all paths allowed",
            '        if !self.inner.has_hooks("stat") {',
            "            return Ok(vec![true; paths.len()]);",
            "        }",
            "",
            "        // Build Python context once for all paths",
            '        let py_ctx = rust_ctx_to_python(py, &ctx.to_rust(), "")',
            "            .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;",
            '        let hc = get_hook_ctx_cache(py).ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("hook context cache init failed"))?;',
            "",
            "        // Cache PermissionDeniedError class for isinstance check",
            "        let perm_denied_cls = get_exception_cache(py)",
            "            .map(|c| c.permission_denied.bind(py));",
            "",
            "        let mut results = Vec::with_capacity(paths.len());",
            "        for path in &paths {",
            "            let shc = hc.stat.bind(py).call1((path.as_str(), &py_ctx, permission))?.unbind();",
            '            match self.dispatch_pre_hooks_inner("stat", &shc) {',
            "                Ok(()) => results.push(true),",
            "                Err(e) => {",
            "                    // PermissionDeniedError or builtin PermissionError → denied;",
            "                    // other errors propagate.  Issue #3786 / Codex Round 10 finding #3:",
            "                    // PermissionChecker.check raises the builtin on normal denials,",
            "                    // so we must catch both classes here or batch_stat would surface",
            "                    // a 500 instead of a per-path False.",
            "                    if let Some(cls) = perm_denied_cls {",
            "                        if e.is_instance(py, cls) {",
            "                            results.push(false);",
            "                            continue;",
            "                        }",
            "                    }",
            "                    if e.is_instance_of::<pyo3::exceptions::PyPermissionError>(py) {",
            "                        results.push(false);",
            "                        continue;",
            "                    }",
            "                    return Err(e);",
            "                }",
            "            }",
            "        }",
            "        Ok(results)",
            "    }",
            "",
            "    // ── sys_read ───────────────────────────────────────────────────────",
            "",
            "    #[pyo3(signature = (path, ctx, timeout_ms=5000, offset=0))]",
            "    fn sys_read<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        path: &str,",
            "        ctx: &PyOperationContext,",
            "        timeout_ms: u64,",
            "        offset: u64,",
            "    ) -> PyResult<PySysReadResult> {",
            "        // 1. PRE-INTERCEPT hooks (GIL, abort on exception)",
            '        if self.inner.has_hooks("read") {',
            "            // Convert Rust ctx to Python OperationContext dataclass (full round-trip)",
            '            let py_ctx = rust_ctx_to_python(py, &ctx.to_rust(), "")',
            "                .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;",
            '            let hc = get_hook_ctx_cache(py).ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("hook context cache init failed"))?;',
            "            let rhc = hc.read.bind(py).call1((path, py_ctx))?.unbind();",
            '            self.dispatch_pre_hooks_inner("read", &rhc)?;',
            "        }",
            "",
            "        // 2. Call pure Rust kernel (releasing GIL for VFS lock blocking)",
            "        let rust_ctx = ctx.to_rust();",
            "        let result = py.detach(|| self.inner.sys_read_one(path, &rust_ctx, timeout_ms, offset));",
            "        let result = result.map_err(|e| -> PyErr { e.into() })?;",
            "",
            "        // 3. Convert Vec<u8> -> PyBytes",
            "        Ok(PySysReadResult {",
            "            data: result.data.map(|d| PyBytes::new(py, &d).into()),",
            "            post_hook_needed: result.post_hook_needed,",
            "            content_id: result.content_id,",
            "            gen: result.gen,",
            "            entry_type: result.entry_type,",
            "            stream_next_offset: result.stream_next_offset,",
            "        })",
            "    }",
            "",
            "    // ── sys_write ──────────────────────────────────────────────────────",
            "",
            "    #[pyo3(signature = (path, ctx, content, offset=0))]",
            "    fn sys_write<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        path: &str,",
            "        ctx: &PyOperationContext,",
            "        content: &[u8],",
            "        offset: u64,",
            "    ) -> PyResult<PySysWriteResult> {",
            "        // 1. PRE-INTERCEPT hooks (GIL, abort on exception)",
            '        if self.inner.has_hooks("write") {',
            "            // Convert Rust ctx to Python OperationContext dataclass (full round-trip)",
            '            let py_ctx = rust_ctx_to_python(py, &ctx.to_rust(), "")',
            "                .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;",
            '            let hc = get_hook_ctx_cache(py).ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("hook context cache init failed"))?;',
            "            let whc = hc.write.bind(py).call1((path, content, py_ctx))?.unbind();",
            '            self.dispatch_pre_hooks_inner("write", &whc)?;',
            "        }",
            "",
            "        // 2. Call pure Rust kernel (releasing GIL for VFS lock blocking)",
            "        let rust_ctx = ctx.to_rust();",
            "        let content_owned = content.to_vec();",
            "        let result = py.detach(|| self.inner.sys_write_one(path, &rust_ctx, &content_owned, offset));",
            "        let result = result.map_err(|e| -> PyErr { e.into() })?;",
            "",
            "        Ok(PySysWriteResult {",
            "            hit: result.hit,",
            "            content_id: result.content_id,",
            "            post_hook_needed: result.post_hook_needed,",
            "            version: result.version,",
            "            gen: result.gen,",
            "            size: result.size,",
            "            is_new: result.is_new,",
            "            old_content_id: result.old_content_id,",
            "            old_size: result.old_size,",
            "            old_version: result.old_version,",
            "            old_modified_at_ms: result.old_modified_at_ms,",
            "        })",
            "    }",
            "",
            "    // ── write-buffer flushing ─────────────────────────────────────────",
            "",
            "    #[pyo3(signature = (path=None, zone_id=None))]",
            "    fn flush_write_buffer(",
            "        &self,",
            "        py: Python<'_>,",
            "        path: Option<&str>,",
            "        zone_id: Option<&str>,",
            "    ) -> PyResult<PyFlushWriteBufferResult> {",
            "        let result = py.detach(|| self.inner.flush_write_buffer(path, zone_id));",
            "        let result = result.map_err(|e| -> PyErr { e.into() })?;",
            "        Ok(PyFlushWriteBufferResult {",
            "            flushed: result.flushed,",
            "            failed: result.failed,",
            "            errors: result.errors,",
            "        })",
            "    }",
            "",
            "    fn flush_due_write_buffer(",
            "        &self,",
            "        py: Python<'_>,",
            "    ) -> PyResult<PyFlushWriteBufferResult> {",
            "        let result = py.detach(|| self.inner.flush_due_write_buffer());",
            "        let result = result.map_err(|e| -> PyErr { e.into() })?;",
            "        Ok(PyFlushWriteBufferResult {",
            "            flushed: result.flushed,",
            "            failed: result.failed,",
            "            errors: result.errors,",
            "        })",
            "    }",
            "",
            "    // ── sys_stat ───────────────────────────────────────────────────────",
            "",
            "    fn sys_stat<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        path: &str,",
            "        zone_id: &str,",
            "    ) -> PyResult<Option<Bound<'py, PyDict>>> {",
            "        match self.inner.sys_stat(path, zone_id) {",
            "            Some(s) => {",
            "                let dict = PyDict::new(py);",
            '                dict.set_item("path", &s.path)?;',
            '                dict.set_item("size", s.size)?;',
            '                dict.set_item("last_writer_address", s.last_writer_address.as_deref())?;',
            '                dict.set_item("content_id", s.content_id.as_deref())?;',
            '                dict.set_item("mime_type", &s.mime_type)?;',
            "                set_optional_iso_datetime(",
            '                    py, &dict, "created_at", s.created_at_ms,',
            "                )?;",
            "                set_optional_iso_datetime(",
            '                    py, &dict, "modified_at", s.modified_at_ms,',
            "                )?;",
            '                dict.set_item("is_directory", s.is_directory)?;',
            '                dict.set_item("entry_type", s.entry_type)?;',
            '                dict.set_item("mode", s.mode)?;',
            '                dict.set_item("version", s.version)?;',
            '                dict.set_item("gen", s.gen)?;',
            '                dict.set_item("zone_id", s.zone_id.as_deref())?;',
            '                dict.set_item("link_target", s.link_target.as_deref())?;',
            "                match &s.lock {",
            "                    Some(lock) => {",
            "                        let lock_dict = PyDict::new(py);",
            '                        let label = if lock.max_holders == 1 { "mutex" } else { "semaphore" };',
            '                        lock_dict.set_item("mode", label)?;',
            '                        lock_dict.set_item("max_holders", lock.max_holders)?;',
            "                        let holders = pyo3::types::PyList::empty(py);",
            "                        for h in &lock.holders {",
            "                            let h_dict = PyDict::new(py);",
            '                            h_dict.set_item("lock_id", &h.lock_id)?;',
            '                            h_dict.set_item("holder_info", &h.holder_info)?;',
            '                            h_dict.set_item("acquired_at", h.acquired_at_secs)?;',
            '                            h_dict.set_item("expires_at", h.expires_at_secs)?;',
            "                            holders.append(h_dict)?;",
            "                        }",
            '                        lock_dict.set_item("holders", holders)?;',
            '                        dict.set_item("lock", lock_dict)?;',
            "                    }",
            "                    None => {",
            '                        dict.set_item("lock", py.None())?;',
            "                    }",
            "                }",
            "                Ok(Some(dict))",
            "            }",
            "            None => Ok(None),",
            "        }",
            "    }",
            "",
            "    // ── sys_unlink ────────────────────────────────────────────────────",
            "",
            "    #[pyo3(signature = (path, ctx, recursive=false))]",
            "    fn sys_unlink(",
            "        &self,",
            "        py: Python<'_>,",
            "        path: &str,",
            "        ctx: &PyOperationContext,",
            "        recursive: bool,",
            "    ) -> PyResult<PySysUnlinkResult> {",
            "        // 1. PRE-INTERCEPT hooks (GIL, abort on exception)",
            '        if self.inner.has_hooks("delete") {',
            '            let py_ctx = rust_ctx_to_python(py, &ctx.to_rust(), "")',
            "                .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;",
            '            let hc = get_hook_ctx_cache(py).ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("hook context cache init failed"))?;',
            "            let dhc = hc.delete.bind(py).call1((path, py_ctx))?.unbind();",
            '            self.dispatch_pre_hooks_inner("delete", &dhc)?;',
            "        }",
            "",
            "        // 2. Call pure Rust kernel",
            "        let rust_ctx = ctx.to_rust();",
            "        let result = py.detach(|| self.inner.sys_unlink_one(path, &rust_ctx, recursive));",
            "        let result = result.map_err(|e| -> PyErr { e.into() })?;",
            "",
            "        Ok(PySysUnlinkResult {",
            "            hit: result.hit,",
            "            entry_type: result.entry_type,",
            "            post_hook_needed: result.post_hook_needed,",
            "            path: result.path,",
            "            content_id: result.content_id,",
            "            size: result.size,",
            "        })",
            "    }",
            "",
            "    // ── sys_rename ────────────────────────────────────────────────────",
            "",
            "    #[pyo3(signature = (old_path, new_path, ctx))]",
            "    fn sys_rename(",
            "        &self,",
            "        py: Python<'_>,",
            "        old_path: &str,",
            "        new_path: &str,",
            "        ctx: &PyOperationContext,",
            "    ) -> PyResult<PySysRenameResult> {",
            "        // 1. PRE-INTERCEPT hooks (GIL, abort on exception)",
            '        if self.inner.has_hooks("rename") {',
            '            let py_ctx = rust_ctx_to_python(py, &ctx.to_rust(), "")',
            "                .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;",
            '            let hc = get_hook_ctx_cache(py).ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("hook context cache init failed"))?;',
            "            let rhc = hc.rename.bind(py).call1((old_path, new_path, py_ctx))?.unbind();",
            '            self.dispatch_pre_hooks_inner("rename", &rhc)?;',
            "        }",
            "",
            "        // 2. Call pure Rust kernel",
            "        let rust_ctx = ctx.to_rust();",
            "        let result = py.detach(|| self.inner.sys_rename(old_path, new_path, &rust_ctx));",
            "        let result = result.map_err(|e| -> PyErr { e.into() })?;",
            "",
            "        Ok(PySysRenameResult {",
            "            hit: result.hit,",
            "            success: result.success,",
            "            post_hook_needed: result.post_hook_needed,",
            "            is_directory: result.is_directory,",
            "            old_content_id: result.old_content_id,",
            "            old_size: result.old_size,",
            "            old_version: result.old_version,",
            "            old_modified_at_ms: result.old_modified_at_ms,",
            "        })",
            "    }",
            "",
            "    // ── sys_copy ──────────────────────────────────────────────────────",
            "",
            "    #[pyo3(signature = (src_path, dst_path, ctx))]",
            "    fn sys_copy(",
            "        &self,",
            "        py: Python<'_>,",
            "        src_path: &str,",
            "        dst_path: &str,",
            "        ctx: &PyOperationContext,",
            "    ) -> PyResult<PySysCopyResult> {",
            "        // 1. PRE-INTERCEPT hooks (GIL, abort on exception)",
            '        if self.inner.has_hooks("copy") {',
            '            let py_ctx = rust_ctx_to_python(py, &ctx.to_rust(), "")',
            "                .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;",
            '            let hc = get_hook_ctx_cache(py).ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("hook context cache init failed"))?;',
            "            let chc = hc.copy.bind(py).call1((src_path, dst_path, py_ctx))?.unbind();",
            '            self.dispatch_pre_hooks_inner("copy", &chc)?;',
            "        }",
            "",
            "        // 2. Call pure Rust kernel",
            "        let rust_ctx = ctx.to_rust();",
            "        let result = py.detach(|| self.inner.sys_copy(src_path, dst_path, &rust_ctx));",
            "        let result = result.map_err(|e| -> PyErr { e.into() })?;",
            "",
            "        Ok(PySysCopyResult {",
            "            hit: result.hit,",
            "            post_hook_needed: result.post_hook_needed,",
            "            dst_path: result.dst_path,",
            "            content_id: result.content_id,",
            "            size: result.size,",
            "            version: result.version,",
            "            gen: result.gen,",
            "        })",
            "    }",
            "",
            "    // ── sys_mkdir ─────────────────────────────────────────────────────",
            "",
            "    #[pyo3(signature = (path, ctx, parents=true, exist_ok=true))]",
            "    fn sys_mkdir(",
            "        &self,",
            "        py: Python<'_>,",
            "        path: &str,",
            "        ctx: &PyOperationContext,",
            "        parents: bool,",
            "        exist_ok: bool,",
            "    ) -> PyResult<PySysMkdirResult> {",
            "        // 1. PRE-INTERCEPT hooks (GIL, abort on exception)",
            '        if self.inner.has_hooks("mkdir") {',
            '            let py_ctx = rust_ctx_to_python(py, &ctx.to_rust(), "")',
            "                .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;",
            '            let hc = get_hook_ctx_cache(py).ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("hook context cache init failed"))?;',
            "            let mhc = hc.mkdir.bind(py).call1((path, py_ctx))?.unbind();",
            '            self.dispatch_pre_hooks_inner("mkdir", &mhc)?;',
            "        }",
            "",
            "        // 2. Call pure Rust kernel (full mkdir)",
            "        let rust_ctx = ctx.to_rust();",
            "        let result = py.detach(|| self.inner.sys_mkdir(path, &rust_ctx, parents, exist_ok));",
            "        let result = result.map_err(|e| -> PyErr { e.into() })?;",
            "",
            "        Ok(PySysMkdirResult {",
            "            hit: result.hit,",
            "            post_hook_needed: result.post_hook_needed,",
            "        })",
            "    }",
            "",
            "    // ── sys_rmdir ─────────────────────────────────────────────────────",
            "",
            "    #[pyo3(signature = (path, ctx, recursive=false))]",
            "    fn sys_rmdir(",
            "        &self,",
            "        py: Python<'_>,",
            "        path: &str,",
            "        ctx: &PyOperationContext,",
            "        recursive: bool,",
            "    ) -> PyResult<PySysRmdirResult> {",
            "        // 1. PRE-INTERCEPT hooks (GIL, abort on exception)",
            '        if self.inner.has_hooks("rmdir") {',
            '            let py_ctx = rust_ctx_to_python(py, &ctx.to_rust(), "")',
            "                .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;",
            '            let hc = get_hook_ctx_cache(py).ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("hook context cache init failed"))?;',
            "            let rhc = hc.rmdir.bind(py).call1((path, py_ctx))?.unbind();",
            '            self.dispatch_pre_hooks_inner("rmdir", &rhc)?;',
            "        }",
            "",
            "        // 2. Call pure Rust kernel (full rmdir)",
            "        let rust_ctx = ctx.to_rust();",
            "        let result = py.detach(|| self.inner.sys_rmdir(path, &rust_ctx, recursive));",
            "        let result = result.map_err(|e| -> PyErr { e.into() })?;",
            "",
            "        Ok(PySysRmdirResult {",
            "            hit: result.hit,",
            "            post_hook_needed: result.post_hook_needed,",
            "            children_deleted: result.children_deleted,",
            "        })",
            "    }",
            "",
            "    // ── Tier 2 convenience methods ────────────────────────────────────",
            "",
            "    #[pyo3(signature = (path, zone_id))]",
            "    fn access(&self, path: &str, zone_id: &str) -> bool {",
            "        self.inner.access(path, zone_id)",
            "    }",
            "",
            "    #[pyo3(signature = (parent_path, zone_id, is_admin=false))]",
            "    fn readdir(&self, parent_path: &str, zone_id: &str, is_admin: bool) -> Vec<(String, u8)> {",
            "        self.inner.readdir(parent_path, zone_id, is_admin)",
            "    }",
            "",
            "    /// Backend-native directory listing for external connector mounts.",
            "    fn sys_readdir_backend(&self, path: &str, zone_id: &str) -> Vec<String> {",
            "        self.inner.sys_readdir_backend(path, zone_id)",
            "    }",
            "",
            "    /// Phase 6: glob match against the metastore-recursive listing of",
            "    /// `prefix`.  Replaces `nexus.fs._helpers.glob` — pure Rust, no",
            "    /// Python fallback (`lib::glob::glob_match` covers the same",
            "    /// `globset` syntax the Python `fnmatch` fallback used).",
            '    #[pyo3(signature = (pattern, prefix="/", zone_id="root"))]',
            "    fn sys_glob(",
            "        &self,",
            "        pattern: &str,",
            "        prefix: &str,",
            "        zone_id: &str,",
            "    ) -> PyResult<Vec<String>> {",
            '        let ctx = OperationContext::new("system", zone_id, true, None, true);',
            "        self.inner",
            "            .sys_glob(pattern, prefix, &ctx)",
            '            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("sys_glob: {:?}", e)))',
            "    }",
            "",
            "    /// Phase 6: grep recursive — walk every regular file under",
            "    /// `prefix`, scan content via `lib::search::search_lines`, return",
            "    /// up to `max_results` matches.  Replaces `nexus.fs._helpers.grep`.",
            "    /// Each match comes back as a `dict` matching the historical",
            "    /// shape: `{file, line, content, match}`.",
            "    ///",
            "    /// When `disk_paths` is non-empty the metastore walk is skipped:",
            "    /// the kernel reads each absolute path from disk directly.  Used",
            "    /// by the search-tier cache fast path where the cached blob's",
            "    /// on-disk location is already known.",
            '    #[pyo3(signature = (pattern, prefix="/", ignore_case=false, max_results=1000, zone_id="root", disk_paths=None))]',
            "    #[allow(clippy::too_many_arguments)]",
            "    fn sys_grep<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        pattern: &str,",
            "        prefix: &str,",
            "        ignore_case: bool,",
            "        max_results: usize,",
            "        zone_id: &str,",
            "        disk_paths: Option<Vec<String>>,",
            "    ) -> PyResult<Bound<'py, PyList>> {",
            '        let ctx = OperationContext::new("system", zone_id, true, None, true);',
            "        let paths = disk_paths.unwrap_or_default();",
            "        let matches = self.inner",
            "            .sys_grep(pattern, prefix, ignore_case, max_results, &paths, &ctx)",
            '            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("sys_grep: {:?}", e)))?;',
            "        let out = PyList::empty(py);",
            "        for m in matches {",
            "            let d = PyDict::new(py);",
            '            d.set_item("file", m.file)?;',
            '            d.set_item("line", m.line)?;',
            '            d.set_item("content", m.content)?;',
            '            d.set_item("match", m.match_text)?;',
            "            out.append(d)?;",
            "        }",
            "        Ok(out)",
            "    }",
            "",
            "    /// Simplified sys_read that takes (path, zone_id) — creates a minimal",
            "    /// OperationContext internally.  Used by service-tier callers that don't",
            "    /// have a full OperationContext handy.",
            "    fn sys_read_raw<'py>(&self, py: Python<'py>, path: &str, zone_id: &str) -> PyResult<Py<PyAny>> {",
            '        let ctx = OperationContext::new("system", zone_id, true, None, true);',
            "        let result = self.inner.sys_read_one(path, &ctx, 5000, 0).map_err(|e| {",
            '            pyo3::exceptions::PyRuntimeError::new_err(format!("sys_read_raw: {:?}", e))',
            "        })?;",
            "        match result.data {",
            "            Some(bytes) => Ok(pyo3::types::PyBytes::new(py, &bytes).into()),",
            "            None => Err(pyo3::exceptions::PyFileNotFoundError::new_err(",
            '                format!("File not found: {}", path),',
            "            )),",
            "        }",
            "    }",
            "",
            '    #[pyo3(signature = (path, zone_id="root", strict_json=true))]',
            "    fn sys_cat<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        path: &str,",
            "        zone_id: &str,",
            "        strict_json: bool,",
            "    ) -> PyResult<Py<PyBytes>> {",
            '        let ctx = OperationContext::new("system", zone_id, true, None, true);',
            "        let result = self",
            "            .inner",
            "            .sys_cat(path, &ctx, strict_json)",
            "            .map_err(|e| -> PyErr { e.into() })?;",
            "        Ok(PyBytes::new(py, &result.data).unbind())",
            "    }",
            "",
            '    #[pyo3(signature = (path, zone_id="root"))]',
            "    fn op_metadata_for_path<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        path: &str,",
            "        zone_id: &str,",
            "    ) -> PyResult<Bound<'py, PyDict>> {",
            '        let ctx = OperationContext::new("system", zone_id, true, None, true);',
            "        let result = self",
            "            .inner",
            "            .op_metadata_for_path(path, &ctx)",
            "            .map_err(|e| -> PyErr { e.into() })?;",
            "        let dict = PyDict::new(py);",
            '        dict.set_item("filetype", result.filetype.as_str())?;',
            '        dict.set_item("backend", result.backend.as_str())?;',
            '        dict.set_item("mime_type", result.mime_type.as_deref())?;',
            '        dict.set_item("backend_name", result.backend_name)?;',
            "        Ok(dict)",
            "    }",
            "",
            '    #[pyo3(signature = (path, zone_id="root"))]',
            "    fn backend_fingerprint(",
            "        &self,",
            "        path: &str,",
            "        zone_id: &str,",
            "    ) -> PyResult<Option<String>> {",
            '        let ctx = OperationContext::new("system", zone_id, true, None, true);',
            "        self.inner",
            "            .backend_fingerprint(path, &ctx)",
            "            .map_err(|e| -> PyErr { e.into() })",
            "    }",
            "",
            "    // ── Batch syscalls ─────────────────────────────────────────────────",
            "",
            "    /// Batch write: validate + route + lock + write + metastore + dcache.",
            "    /// Returns list of SysWriteResult (one per item).",
            "    #[pyo3(signature = (items, ctx))]",
            "    fn sys_write_batch<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        items: Vec<(String, Vec<u8>)>,",
            "        ctx: &PyOperationContext,",
            "    ) -> PyResult<Vec<PySysWriteResult>> {",
            "        let rust_ctx = ctx.to_rust();",
            "        let reqs: Vec<crate::kernel::WriteRequest> = items",
            "            .into_iter()",
            "            .map(|(path, content)| crate::kernel::WriteRequest { path, content, offset: 0 })",
            "            .collect();",
            "        let results = py.detach(|| self.inner.sys_write(&reqs, &rust_ctx));",
            "        Ok(results",
            "            .into_iter()",
            "            .map(|r| {",
            "                let r = match r {",
            "                    Ok(r) => r,",
            "                    Err(_) => crate::kernel::SysWriteResult {",
            "                        hit: false, content_id: None, post_hook_needed: false,",
            "                        version: 0, gen: 0, size: 0, is_new: false,",
            "                        old_content_id: None, old_size: None, old_version: None,",
            "                        old_modified_at_ms: None,",
            "                    },",
            "                };",
            "                PySysWriteResult {",
            "                    hit: r.hit,",
            "                    content_id: r.content_id,",
            "                    post_hook_needed: r.post_hook_needed,",
            "                    version: r.version,",
            "                    gen: r.gen,",
            "                    size: r.size,",
            "                    is_new: r.is_new,",
            "                    old_content_id: r.old_content_id,",
            "                    old_size: r.old_size,",
            "                    old_version: r.old_version,",
            "                    old_modified_at_ms: r.old_modified_at_ms,",
            "                }",
            "            })",
            "            .collect())",
            "    }",
            "",
            "    /// Batch read. Accepts either `list[str]` (legacy) or",
            "    /// `list[tuple[str, int, int | None]]`. Returns `list[PyBatchReadItem]`.",
            "    #[pyo3(signature = (reqs, ctx))]",
            "    fn sys_read_batch<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        reqs: Bound<'py, PyAny>,",
            "        ctx: &PyOperationContext,",
            "    ) -> PyResult<Vec<PyBatchReadItem>> {",
            "        // Parse either shape: list[str] (legacy) or list[(str, int, int|None)].",
            "        let rust_reqs: Vec<crate::kernel::ReadRequest> = if let Ok(paths) =",
            "            reqs.extract::<Vec<String>>()",
            "        {",
            "            paths",
            "                .into_iter()",
            "                .map(|p| crate::kernel::ReadRequest {",
            "                    path: p,",
            "                    offset: 0,",
            "                    len: None,",
            "                    timeout_ms: 5000,",
            "                })",
            "                .collect()",
            "        } else {",
            "            let tuples: Vec<(String, u64, Option<u64>)> = reqs.extract()?;",
            "            tuples",
            "                .into_iter()",
            "                .map(|(p, off, len)| crate::kernel::ReadRequest {",
            "                    path: p,",
            "                    offset: off,",
            "                    len,",
            "                    timeout_ms: 5000,",
            "                })",
            "                .collect()",
            "        };",
            "        let rust_ctx = ctx.to_rust();",
            "        let results = py.detach(|| self.inner.sys_read(&rust_reqs, &rust_ctx));",
            "        Ok(results",
            "            .into_iter()",
            "            .map(|r| match r {",
            "                Ok(r) => PyBatchReadItem {",
            "                    data: r.data.map(|d| PyBytes::new(py, &d).into()),",
            "                    content_id: r.content_id,",
            "                    gen: r.gen,",
            "                    entry_type: r.entry_type,",
            "                    post_hook_needed: r.post_hook_needed,",
            "                    error_kind: String::new(),",
            "                    error_message: String::new(),",
            "                },",
            "                Err(e) => {",
            "                    let (kind, msg) = crate::batch_read_py::batch_err_kind_msg(&e);",
            "                    PyBatchReadItem {",
            "                        data: None,",
            "                        content_id: None,",
            "                        gen: 0,",
            "                        entry_type: 0,",
            "                        post_hook_needed: false,",
            "                        error_kind: kind,",
            "                        error_message: msg,",
            "                    }",
            "                }",
            "            })",
            "            .collect())",
            "    }",
            "",
            "    /// Batch delete: loops sys_unlink for each path.",
            "    /// Returns list of SysUnlinkResult (one per path).",
            "    #[pyo3(signature = (paths, ctx))]",
            "    fn sys_unlink_batch(",
            "        &self,",
            "        py: Python<'_>,",
            "        paths: Vec<String>,",
            "        ctx: &PyOperationContext,",
            "    ) -> PyResult<Vec<PySysUnlinkResult>> {",
            "        let rust_ctx = ctx.to_rust();",
            "        let reqs: Vec<crate::kernel::UnlinkRequest> = paths",
            "            .into_iter()",
            "            .map(|path| crate::kernel::UnlinkRequest { path, recursive: false })",
            "            .collect();",
            "        let results = py.detach(|| self.inner.sys_unlink(&reqs, &rust_ctx));",
            "        Ok(results",
            "            .into_iter()",
            "            .map(|r| {",
            "                let r = match r {",
            "                    Ok(r) => r,",
            "                    Err(_) => crate::kernel::SysUnlinkResult {",
            "                        hit: false, entry_type: 0, post_hook_needed: false,",
            "                        path: String::new(), content_id: None, size: 0,",
            "                    },",
            "                };",
            "                PySysUnlinkResult {",
            "                    hit: r.hit,",
            "                    entry_type: r.entry_type,",
            "                    post_hook_needed: r.post_hook_needed,",
            "                    path: r.path,",
            "                    content_id: r.content_id,",
            "                    size: r.size,",
            "                }",
            "            })",
            "            .collect())",
            "    }",
            "",
            "    // ── Zone revision counter (§10 A2) ──────────────────────────────",
            "",
            "    /// Increment zone revision (called after successful metastore write).",
            "    fn increment_zone_revision(&self, zone_id: &str) -> u64 {",
            "        self.inner.increment_zone_revision(zone_id)",
            "    }",
            "",
            "    /// Notify a specific zone revision (monotonic update).",
            "    fn notify_zone_revision(&self, zone_id: &str, revision: u64) {",
            "        self.inner.notify_zone_revision(zone_id, revision)",
            "    }",
            "",
            "    /// Get current zone revision (0 if unknown).",
            "    fn get_zone_revision(&self, zone_id: &str) -> u64 {",
            "        self.inner.get_zone_revision(zone_id)",
            "    }",
            "",
            "    /// Wait until zone revision >= min_revision, or timeout.",
            "    /// Releases GIL during condvar wait (pure Rust blocking).",
            "    fn wait_zone_revision(&self, py: Python<'_>, zone_id: &str, min_revision: u64, timeout_ms: u64) -> bool {",
            "        let zone = zone_id.to_string();",
            "        py.detach(|| self.inner.wait_zone_revision(&zone, min_revision, timeout_ms))",
            "    }",
            "",
            "    // ── File watch registry (§10 A3) ────────────────────────────────",
            "",
            "    /// Register a glob pattern watch. Returns watch ID.",
            "    fn register_watch(&self, pattern: &str) -> u64 {",
            "        self.inner.register_watch(pattern)",
            "    }",
            "",
            "    /// Unregister a file watch by ID.",
            "    fn unregister_watch(&self, watch_id: u64) -> bool {",
            "        self.inner.unregister_watch(watch_id)",
            "    }",
            "",
            "    /// Match a path against all registered watch patterns.",
            "    fn match_watches(&self, path: &str) -> Vec<u64> {",
            "        self.inner.match_watches(path)",
            "    }",
            "",
            "    // ── Agent registry (§10 B1) ─────────────────────────────────────",
            "",
            "    /// Sub-handle exposing the kernel `AgentRegistry` SSOT to",
            "    /// Python callers. Wraps the kernel `Arc<AgentRegistry>`",
            "    /// — every call returns a fresh wrapper sharing the same",
            "    /// state, so Python code can write",
            "    /// `kernel.agent_registry.spawn(...)` without manual Arc",
            "    /// management.",
            "    #[getter]",
            "    fn agent_registry(&self) -> crate::agent_registry_py::PyAgentRegistry {",
            "        crate::agent_registry_py::from_kernel(&self.inner)",
            "    }",
            "",
            "    /// Register a new agent. Returns true if inserted (pid was new).",
            "    #[pyo3(signature = (pid, name, kind, owner_id, zone_id, created_at_ms, parent_pid=None, connection_id=None))]",
            "    #[allow(clippy::too_many_arguments)]",
            "    fn agent_register(",
            "        &self,",
            "        pid: &str,",
            "        name: &str,",
            "        kind: &str,",
            "        owner_id: &str,",
            "        zone_id: &str,",
            "        created_at_ms: u64,",
            "        parent_pid: Option<&str>,",
            "        connection_id: Option<&str>,",
            "    ) -> bool {",
            "        use crate::core::agents::registry::{AgentDescriptor, AgentKind};",
            "        let kind = AgentKind::from_str(kind).unwrap_or(AgentKind::Worker);",
            "        self.inner.agent_registry.register(AgentDescriptor {",
            "            pid: pid.to_string(),",
            "            name: name.to_string(),",
            "            kind,",
            "            owner_id: owner_id.to_string(),",
            "            zone_id: zone_id.to_string(),",
            "            created_at_ms,",
            "            updated_at_ms: created_at_ms,",
            "            parent_pid: parent_pid.map(|s| s.to_string()),",
            "            connection_id: connection_id.map(|s| s.to_string()),",
            "            ..Default::default()",
            "        })",
            "    }",
            "",
            "    /// Unregister an agent by pid.",
            "    fn agent_unregister(&self, pid: &str) -> bool {",
            "        self.inner.agent_registry.unregister(pid).is_some()",
            "    }",
            "",
            "    /// Get agent descriptor as dict (legacy; new callers should",
            "    /// use `kernel.agent_registry.get(pid)`).",
            "    fn agent_get<'py>(&self, py: Python<'py>, pid: &str) -> PyResult<Option<Bound<'py, PyDict>>> {",
            "        match self.inner.agent_registry.get(pid) {",
            "            Some(desc) => {",
            "                let dict = PyDict::new(py);",
            '                dict.set_item("pid", &desc.pid)?;',
            '                dict.set_item("name", &desc.name)?;',
            '                dict.set_item("kind", desc.kind.as_str().to_ascii_lowercase())?;',
            '                dict.set_item("state", desc.state.as_str().to_ascii_lowercase())?;',
            '                dict.set_item("owner_id", &desc.owner_id)?;',
            '                dict.set_item("zone_id", &desc.zone_id)?;',
            '                dict.set_item("created_at_ms", desc.created_at_ms)?;',
            '                dict.set_item("updated_at_ms", desc.updated_at_ms)?;',
            '                dict.set_item("exit_code", desc.exit_code)?;',
            '                dict.set_item("generation", desc.generation)?;',
            '                dict.set_item("cwd", &desc.cwd)?;',
            '                dict.set_item("root", &desc.root)?;',
            '                dict.set_item("parent_pid", desc.parent_pid.as_deref())?;',
            '                dict.set_item("connection_id", desc.connection_id.as_deref())?;',
            '                dict.set_item("last_heartbeat_ms", desc.last_heartbeat_ms)?;',
            '                dict.set_item("children", desc.children.clone())?;',
            '                dict.set_item("labels", desc.labels.clone())?;',
            "                Ok(Some(dict))",
            "            }",
            "            None => Ok(None),",
            "        }",
            "    }",
            "",
            "    /// Update agent state. Returns true if found, false if pid",
            "    /// missing. Raises ValueError on invalid transitions.",
            "    fn agent_update_state(&self, pid: &str, new_state: &str) -> PyResult<bool> {",
            "        use crate::core::agents::registry::AgentState;",
            "        let state = AgentState::from_str(new_state).ok_or_else(|| {",
            '                pyo3::exceptions::PyValueError::new_err(format!("unknown agent state: {new_state}"))',
            "            })?;",
            "        self.inner",
            "            .agent_registry",
            "            .update_state(pid, state)",
            "            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))",
            "    }",
            "",
            "    /// List all agents. Returns list of dicts.",
            "    #[pyo3(signature = (zone_id=None, owner_id=None, state=None, kind=None))]",
            "    fn agent_list<'py>(",
            "        &self,",
            "        py: Python<'py>,",
            "        zone_id: Option<&str>,",
            "        owner_id: Option<&str>,",
            "        state: Option<&str>,",
            "        kind: Option<&str>,",
            "    ) -> PyResult<Vec<Bound<'py, PyDict>>> {",
            "        use crate::core::agents::registry::{AgentKind, AgentState};",
            "        let state_filter = state.and_then(AgentState::from_str);",
            "        let kind_filter = kind.and_then(AgentKind::from_str);",
            "        let agents = self.inner.agent_registry.list(",
            "            zone_id,",
            "            owner_id,",
            "            kind_filter.as_ref(),",
            "            state_filter.as_ref(),",
            "        );",
            "        let mut result = Vec::with_capacity(agents.len());",
            "        for desc in agents {",
            "            let dict = PyDict::new(py);",
            '            dict.set_item("pid", &desc.pid)?;',
            '            dict.set_item("name", &desc.name)?;',
            '            dict.set_item("kind", desc.kind.as_str().to_ascii_lowercase())?;',
            '            dict.set_item("state", desc.state.as_str().to_ascii_lowercase())?;',
            '            dict.set_item("owner_id", &desc.owner_id)?;',
            '            dict.set_item("zone_id", &desc.zone_id)?;',
            '            dict.set_item("created_at_ms", desc.created_at_ms)?;',
            "            result.push(dict);",
            "        }",
            "        Ok(result)",
            "    }",
            "",
            "    /// Update heartbeat timestamp for an agent (raw stamp; no",
            "    /// kind/info validation — see ``AgentRegistry.heartbeat`` for",
            "    /// the validating variant).",
            "    fn agent_heartbeat(&self, pid: &str, timestamp_ms: u64) -> bool {",
            "        self.inner.agent_registry.heartbeat_at(pid, timestamp_ms)",
            "    }",
            "",
            "    /// Get number of registered agents.",
            "    fn agent_count(&self) -> usize {",
            "        self.inner.agent_registry.count()",
            "    }",
            "",
            "    /// Block (GIL-free) until agent `pid` reaches `target_state` or timeout.",
            "    ///",
            "    /// Returns the state string on success. Raises `RuntimeError` on",
            '    /// timeout ("timeout") or unknown pid ("not_found").',
            "    fn agent_wait(&self, py: Python<'_>, pid: &str, target_state: &str, timeout_ms: u64) -> PyResult<String> {",
            "        use crate::core::agents::registry::AgentState;",
            "        let target = AgentState::from_str(target_state).ok_or_else(|| {",
            "            pyo3::exceptions::PyValueError::new_err(",
            '                format!("unknown agent state: {target_state}"),',
            "            )",
            "        })?;",
            "        let pid = pid.to_string();",
            "        let registry = std::sync::Arc::clone(&self.inner.agent_registry);",
            "        py.detach(|| {",
            "            registry",
            "                .wait_for_state(&pid, &target, timeout_ms)",
            "                .map_err(pyo3::exceptions::PyRuntimeError::new_err)",
            "        })",
            "    }",
            "",
            "    // ── Service registry ─────────────────────────────────────────────",
            "",
            "    /// Register a service. Wraps the Python instance in PyServiceLifecycle.",
            "    #[pyo3(signature = (name, instance, exports=vec![], allow_overwrite=false))]",
            "    fn service_enlist(",
            "        &self,",
            "        _py: Python<'_>,",
            "        name: &str,",
            "        instance: &Bound<'_, PyAny>,",
            "        exports: Vec<String>,",
            "        allow_overwrite: bool,",
            "    ) -> PyResult<()> {",
            "        // Validate exports on the Python side before wrapping",
            "        for exp in &exports {",
            "            if !instance.hasattr(exp.as_str())? {",
            "                return Err(pyo3::exceptions::PyValueError::new_err(format!(",
            '                    "services: {name:?} declares exports not found on instance: [{exp}]"',
            "                )));",
            "            }",
            "        }",
            "        let lifecycle = Box::new(PyServiceLifecycle(instance.clone().unbind()));",
            "        self.inner",
            "            .register_managed_service(name, lifecycle, exports, allow_overwrite)",
            "            .map_err(pyo3::exceptions::PyValueError::new_err)",
            "    }",
            "",
            "    /// Look up a service by name. Returns the raw Python instance or None.",
            "    fn service_lookup(&self, py: Python<'_>, name: &str) -> Option<Py<PyAny>> {",
            "        let lifecycle = self.inner.service_lookup_managed(name)?;",
            "        let adapter = (&*lifecycle as &dyn std::any::Any).downcast_ref::<PyServiceLifecycle>()?;",
            "        Some(adapter.0.clone_ref(py))",
            "    }",
            "",
            "    /// Hot-swap a service: drain → replace → rehook.",
            "    #[pyo3(signature = (name, new_instance, exports=vec![], timeout_ms=10000))]",
            "    fn service_swap(",
            "        &self,",
            "        _py: Python<'_>,",
            "        name: &str,",
            "        new_instance: &Bound<'_, PyAny>,",
            "        exports: Vec<String>,",
            "        timeout_ms: u64,",
            "    ) -> PyResult<()> {",
            "        // Validate exports",
            "        for exp in &exports {",
            "            if !new_instance.hasattr(exp.as_str())? {",
            "                return Err(pyo3::exceptions::PyValueError::new_err(format!(",
            '                    "services: {name:?} replacement declares invalid exports: [{exp}]"',
            "                )));",
            "            }",
            "        }",
            "        let lifecycle = Box::new(PyServiceLifecycle(new_instance.clone().unbind()));",
            "        self.inner",
            "            .swap_managed_service(name, lifecycle, exports, timeout_ms)",
            "            .map_err(pyo3::exceptions::PyKeyError::new_err)",
            "    }",
            "",
            "    /// Unregister a service.",
            "    fn service_unregister(&self, name: &str) -> bool {",
            "        self.inner.unregister_service(name)",
            "    }",
            "",
            "    /// Start all BackgroundService instances.",
            "    #[pyo3(signature = (timeout_ms=30000))]",
            "    fn service_start_all(&self, timeout_ms: u64) -> PyResult<Vec<String>> {",
            "        self.inner",
            "            .service_start_all(timeout_ms as f64 / 1000.0)",
            "            .map_err(pyo3::exceptions::PyRuntimeError::new_err)",
            "    }",
            "",
            "    /// Stop all BackgroundService instances.",
            "    #[pyo3(signature = (timeout_ms=10000))]",
            "    fn service_stop_all(&self, timeout_ms: u64) -> PyResult<Vec<String>> {",
            "        self.inner",
            "            .service_stop_all(timeout_ms as f64 / 1000.0)",
            "            .map_err(pyo3::exceptions::PyRuntimeError::new_err)",
            "    }",
            "",
            "    /// Close all services with close() method.",
            "    fn service_close_all(&self) {",
            "        self.inner.service_close_all()",
            "    }",
            "",
            "    /// Mark bootstrap complete.",
            "    fn service_mark_bootstrapped(&self) {",
            "        self.inner.service_mark_bootstrapped()",
            "    }",
            "",
            "    /// Diagnostic snapshot: list of dicts.",
            "    fn service_snapshot<'py>(&self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyDict>>> {",
            "        let entries = self.inner.service_snapshot();",
            "        let mut result = Vec::with_capacity(entries.len());",
            "        for (name, type_name, exports) in entries {",
            "            let dict = PyDict::new(py);",
            '            dict.set_item("name", &name)?;',
            '            dict.set_item("type", &type_name)?;',
            "            let exports_list: Vec<&str> = exports.iter().map(|s| s.as_str()).collect();",
            '            dict.set_item("exports", exports_list)?;',
            "            result.push(dict);",
            "        }",
            "        Ok(result)",
            "    }",
            "}",
            "",
            "// ── Private: hook dispatch (wrapper-only) ───────────────────────────────",
            "",
            "impl PyKernel {",
            "    /// Internal pre-hook dispatch (used by Tier 1 syscalls).",
            "    fn dispatch_pre_hooks_inner(&self, op: &str, hook_ctx: &Py<PyAny>) -> PyResult<()> {",
            "        let hooks = self.hooks.read_unconditional();",
            "        let impls = hooks.get_pre_hook_impls(op);",
            "        for hook in impls {",
            "            match op {",
            '                "read" => hook.on_pre_read(hook_ctx)?,',
            '                "write" => hook.on_pre_write(hook_ctx)?,',
            '                "delete" => hook.on_pre_delete(hook_ctx)?,',
            '                "rename" => hook.on_pre_rename(hook_ctx)?,',
            '                "mkdir" => hook.on_pre_mkdir(hook_ctx)?,',
            '                "rmdir" => hook.on_pre_rmdir(hook_ctx)?,',
            '                "copy" => hook.on_pre_copy(hook_ctx)?,',
            '                "stat" => hook.on_pre_stat(hook_ctx)?,',
            '                "access" => hook.on_pre_access(hook_ctx)?,',
            '                "write_batch" => hook.on_pre_write_batch(hook_ctx)?,',
            "                _ => {}",
            "            }",
            "        }",
            "        Ok(())",
            "    }",
            "",
            "    /// Dispatch sync post-hooks via Rust InterceptHook trait.",
            "    ///",
            "    /// Runs sync post-hooks serially, fault-isolated (fire-and-forget).",
            "    /// Async hooks are NOT handled here — Python thin wrapper schedules",
            "    /// them via asyncio.gather (codegen or manual).",
            "    fn dispatch_post_hooks_sync(&self, op: &str, hook_ctx: &Py<PyAny>) {",
            "        let hooks = self.hooks.read_unconditional();",
            "        let impls = hooks.get_post_hook_impls(op);",
            "        for hook in impls {",
            "            match op {",
            '                "read" => hook.on_post_read(hook_ctx),',
            '                "write" => hook.on_post_write(hook_ctx),',
            '                "delete" => hook.on_post_delete(hook_ctx),',
            '                "rename" => hook.on_post_rename(hook_ctx),',
            '                "mkdir" => hook.on_post_mkdir(hook_ctx),',
            '                "rmdir" => hook.on_post_rmdir(hook_ctx),',
            '                "copy" => hook.on_post_copy(hook_ctx),',
            '                "stat" => hook.on_post_stat(hook_ctx),',
            '                "access" => hook.on_post_access(hook_ctx),',
            '                "write_batch" => hook.on_post_write_batch(hook_ctx),',
            "                _ => {}",
            "            }",
            "        }",
            "    }",
            "}",
        ]
    )

    return "\n".join(lines) + "\n"


# ── Orchestrator ──────────────────────────────────────────────────


def collect_all() -> tuple[
    dict[str, list[FuncDef]], dict[str, ClassDef], list[str], list[TraitDef], list[str]
]:
    """Parse all Rust sources and collect definitions.

    Returns (module_functions, classes, class_order, traits, all_export_names).
    """
    # Phase 0: kernel/src/lib.rs's `#[pymodule] fn nexus_runtime` body
    # moved to kernel/src/python.rs (`pub fn register`). The cdylib
    # itself now lives in rust/nexus-cdylib/src/lib.rs and just delegates
    # into per-crate `python::register` fns.
    # Phase H/I: lib::python::register holds all algorithm wrappers
    # (rebac, search, glob, io, prefix, simd, trigram, path_utils,
    # bitmap, bloom, hash). Scan all three files and merge so the
    # codegen sees the full set of `wrap_pyfunction!` / `add_class::<…>`
    # registrations.
    lib_text = (RUST_SRC / "lib.rs").read_text()
    kernel_python_text = ""
    kernel_python_path = RUST_SRC / "python.rs"
    if kernel_python_path.exists():
        kernel_python_text = kernel_python_path.read_text()
    lib_python_text = ""
    lib_python_mod = ROOT / "rust" / "lib" / "src" / "python" / "mod.rs"
    if lib_python_mod.exists():
        lib_python_text = lib_python_mod.read_text()
    # Phase 4 (full): peer-crate `python::register` fns also expose
    # pyclasses / pyfunctions into the same `nexus_runtime` Python
    # module (the cdylib calls `kernel::python::register`,
    # `lib::python::register`, AND e.g. `transport::python::register`,
    # `backends::python::register`, `services::python::register`).
    # Scan each peer's mod.rs so codegen sees the full export set —
    # without this, `start_vfs_grpc_server` / `PyVfsGrpcServerHandle`
    # / `PyFederationClient` (moved to `transport::python::register`
    # in Phase 4) drop out of the generated stubs / kernel_exports.
    peer_python_texts: list[str] = []
    for peer in ("transport", "backends", "services"):
        peer_mod = ROOT / "rust" / peer / "src" / "python" / "mod.rs"
        if peer_mod.exists():
            peer_python_texts.append(peer_mod.read_text())
    # Phase 3 plan #6: tasks pyclasses fold into nexus_runtime cdylib via
    # `services::python::register` → `crate::tasks::register_python(m)`.
    # The intra-crate `add_class::<PyTaskEngine>` calls live in
    # `rust/services/src/tasks/mod.rs`, not the peer's python/mod.rs that
    # the loop above scans. Tag those calls with their module ("tasks")
    # so `_resolve_module_path("tasks")` lands on the right file.
    extra_local_class_exports: list[tuple[str, str]] = []
    tasks_mod = ROOT / "rust" / "services" / "src" / "tasks" / "mod.rs"
    if tasks_mod.exists():
        tasks_text = re.sub(r"//[^\n]*", "", tasks_mod.read_text())
        for m in re.finditer(r"add_class::<(\w+)>", tasks_text):
            extra_local_class_exports.append(("tasks", m.group(1)))
    func_exports, class_exports = parse_lib_exports(
        lib_text
        + "\n"
        + kernel_python_text
        + "\n"
        + lib_python_text
        + "\n"
        + "\n".join(peer_python_texts)
    )
    class_exports.extend(extra_local_class_exports)

    # Build set of exported function names per module
    exported_names: dict[str, set[str]] = {}
    for mod_name, func_name in func_exports:
        exported_names.setdefault(mod_name, set()).add(func_name)

    # Collect functions by module (filtered by lib.rs exports)
    module_functions: dict[str, list[FuncDef]] = {}
    for mod_name, names in exported_names.items():
        rs_path = _resolve_module_path(mod_name)
        if rs_path is None:
            continue
        text = rs_path.read_text()
        all_funcs = parse_pyfunctions(text)
        # Only keep functions that are in the export list, deduplicate by name
        seen: set[str] = set()
        exported: list[FuncDef] = []
        for f in all_funcs:
            if f.name in names and f.name not in seen:
                exported.append(f)
                seen.add(f.name)
        # Preserve lib.rs registration order
        order = [fn for _mod, fn in func_exports if _mod == mod_name]
        exported.sort(key=lambda f: order.index(f.name) if f.name in order else 999)
        module_functions[mod_name] = exported

    # Collect classes
    classes: dict[str, ClassDef] = {}
    class_order: list[str] = []  # preserve lib.rs registration order
    for mod_name, cls_name in class_exports:
        if cls_name in classes:
            continue
        rs_path = _resolve_module_path(mod_name)
        if rs_path is None:
            continue
        text = rs_path.read_text()
        methods = parse_pymethods(text, cls_name)
        fields = parse_pyclass_fields(text, cls_name)
        py_name = parse_pyclass_name(text, cls_name)
        cls = ClassDef(name=cls_name, methods=methods, fields=fields, py_name=py_name)
        classes[cls_name] = cls
        class_order.append(cls_name)

    # Collect traits.  Phase 1 split kernel/src/ into three sibling
    # directories — abc/ (§3 ABC pillars), hal/ (kernel-defined extension
    # interfaces), and core/ (§4 primitives + nested per-pillar trait
    # files for pipe / stream).  The scanner walks every location that
    # can host a `pub trait` declaration; pre-Phase-1 fallbacks
    # (core/traits/, flat dispatch.rs / metastore.rs) are still tried so
    # this script keeps working on older checkouts.
    traits: list[TraitDef] = []
    trait_paths: list[Path] = [
        # §4 primitives that declare their own internal traits.
        RUST_SRC / "core" / "dispatch" / "mod.rs",
        RUST_SRC / "core" / "dispatch" / "hook_registry.rs",
        RUST_SRC / "core" / "meta_store" / "mod.rs",
        # Pre-Phase-1 fallbacks — older checkouts.
        RUST_SRC / "core" / "metastore" / "mod.rs",
        RUST_SRC / "dispatch.rs",
        RUST_SRC / "hook_registry.rs",
        RUST_SRC / "metastore.rs",
        # Phase 1: pipe / stream internal traits relocated next to their
        # primitive impls (was core/traits/{pipe,stream}_backend.rs).
        RUST_SRC / "core" / "pipe" / "backend.rs",
        RUST_SRC / "core" / "stream" / "backend.rs",
    ]
    for rs_path in trait_paths:
        if rs_path.exists():
            traits.extend(parse_traits(rs_path.read_text()))
    # Phase 1: §3 ABC pillars + HAL extension interfaces live in
    # `abc/*.rs` and `hal/*.rs`.  Walk each non-mod.rs file in both
    # directories — that's where ObjectStore / MetaStore / CacheStore /
    # LlmStreamingBackend / PeerBlobClient live now.
    for sub_dir in ("abc", "hal"):
        dir_path = RUST_SRC / sub_dir
        if dir_path.is_dir():
            for trait_path in sorted(dir_path.glob("*.rs")):
                if trait_path.name == "mod.rs":
                    continue
                traits.extend(parse_traits(trait_path.read_text()))
    # Pre-Phase-1 fallback: legacy core/traits/ directory.  Empty after
    # Phase 1 lands but tolerated for older checkouts.
    core_traits_dir = RUST_SRC / "core" / "traits"
    if core_traits_dir.is_dir():
        for trait_path in sorted(core_traits_dir.glob("*.rs")):
            if trait_path.name == "mod.rs":
                continue
            traits.extend(parse_traits(trait_path.read_text()))

    # All export names (for kernel_exports.py) — use Python-visible names
    # (#[pyclass(name = "...")] renaming), not Rust struct names.
    all_names: list[str] = []
    for _mod, func_name in func_exports:
        all_names.append(func_name)
    for _mod, cls_name in class_exports:
        cls_def = classes.get(cls_name)
        py_name = cls_def.py_name if cls_def and cls_def.py_name else cls_name
        all_names.append(py_name)

    return module_functions, classes, class_order, traits, all_names


# ── Kernel syscall dispatch generation (Rust SSOT thin wrapper) ───
#
# Replaces the legacy multi-layer Python RPC plumbing (dispatch.py
# table → handlers/filesystem.py wrappers → nexus_fs methods) for
# kernel syscalls.  The gRPC servicer's `Call` handler consults the
# generated dispatcher BEFORE falling through to the (deprecated)
# Python ``dispatch_method`` path.  Each syscall is scanned out of
# the PyKernel ``#[pymethods]`` block in
# ``generated_kernel_abi_pyo3.rs`` — Rust is SSOT, this is a thin
# projection.
#
# The generated module is small and intentionally schema-free at
# call sites: dispatch uses ``inspect.signature`` to filter kwargs
# from the wire dict against the NexusFS method, so adding a new
# kernel syscall only requires extending KERNEL_SYSCALL_NAMES below
# (re-running the codegen) — no params dataclass, no handle_*
# wrapper, no @rpc_expose decorator chain.


# Methods on PyKernel that map 1:1 to a NexusFS syscall and should be
# served by the thin dispatcher.  Names match PyKernel's Python-facing
# names — alias surface (read/write/delete/exists/list/rename/sys_mkdir
# /sys_rmdir/lock_acquire) is wired separately below since some don't
# have a matching PyKernel symbol.
_KERNEL_SYSCALL_ALLOWLIST: tuple[str, ...] = (
    "sys_read",
    "sys_write",
    "sys_setattr",
    "sys_unlink",
    "sys_rename",
    "sys_copy",
    "sys_stat",
    "sys_readdir",
    "sys_mkdir",
    "sys_rmdir",
    "sys_lock",
    "sys_unlock",
    "access",
    "is_directory",
)


# Wire-name → canonical NexusFS method name.  Reasons:
#   1. Aliases (``read`` / ``write`` / ``delete`` / ``exists`` /
#      ``list`` / ``rename``) are short forms used by nexus-test and
#      remote clients; canonical NexusFS methods keep the ``sys_``
#      prefix (or are Tier 2 wrappers without it for mkdir/rmdir).
#   2. PyKernel exposes ``sys_mkdir`` / ``sys_rmdir`` while NexusFS
#      Tier 2 wrappers expose ``mkdir`` / ``rmdir`` (no ``sys_``
#      prefix).  Backward-compat ``sys_mkdir`` / ``sys_rmdir`` keep
#      working for older clients via the alias map.
#   3. ``write`` (and the syscall-shaped ``sys_write`` alias) routes to
#      ``NexusFS.write``: that's the HTTP-style write returning a
#      content_id dict — the wire shape Tier 2 RPC callers depend on
#      through the legacy ``handle_write`` wrapper, including OCC
#      (if_match / if_none_match) handling.
#   4. ``lock_acquire`` is the Tier 2 dict-shaped wrapper — see the
#      Python helper that materializes the ``{acquired, lock_id}``
#      response from ``sys_lock``.
_KERNEL_SYSCALL_ALIASES: dict[str, str] = {
    "delete": "sys_unlink",
    "rename": "sys_rename",
    "exists": "access",
    "list": "sys_readdir",
    # #4005 round-2: ``sys_readdir`` is the canonical wire name CLI clients
    # use directly (no rewrite). PyKernel exposes only ``sys_readdir_backend``
    # so the allowlist scan filters it out — register the identity alias here
    # so KERNEL_SYSCALL_NAMES (built from alias keys) includes it and the
    # gRPC servicer routes ``sys_readdir`` straight to NexusFS.sys_readdir
    # rather than dropping it into the legacy "Unknown method" path.
    "sys_readdir": "sys_readdir",
    # PyKernel says sys_mkdir / sys_rmdir; NexusFS Tier 2 says mkdir / rmdir.
    "sys_mkdir": "mkdir",
    "sys_rmdir": "rmdir",
    # Bare ``mkdir`` / ``rmdir`` are the canonical wire forms used by
    # nexus-test, the Python remote proxy, and the docker E2E suite.  They
    # carry through directly to the same NexusFS Tier 2 methods.  Without
    # these entries the gRPC ``Call`` handler would 404 on bare ``mkdir`` —
    # caught by the federation E2E concurrent-create test.
    "mkdir": "mkdir",
    "rmdir": "rmdir",
    # ``sys_write`` wire name routes to ``NexusFS.write`` (Tier 2 with
    # content_id dict return) — preserves the legacy ``handle_write``
    # wire shape, including OCC (if_match / if_none_match) handling.
    # The Tier 2 ``write`` RPC name does the same.
    "sys_write": "write",
    "write": "write",
    # ``sys_read`` (POSIX) → ``NexusFS.sys_read`` (bytes); ``read``
    # (Tier 2) → ``NexusFS.read`` (return_metadata-aware).  Both wire
    # names exist; the alias map only needs ``read`` because
    # ``sys_read`` resolves to itself.
    "read": "read",
    # Tier 2 lock_acquire dispatches to sys_lock and shapes the result.
    "lock_acquire": "sys_lock",
    "flush_write_buffer": "flush_write_buffer",
    "fsync": "fsync",
    "sync": "sync",
}


# Public NexusFS methods that are not direct PyKernel syscall names but
# should still be served by the thin gRPC syscall dispatcher.
_EXTRA_KERNEL_SYSCALL_NAMES: set[str] = {"flush_write_buffer", "fsync", "sync"}


# Mutation syscalls fire pub/sub events for file watchers via the gRPC
# servicer's ``subscription_manager``.  Each entry: wire-form name →
# event metadata.  Mirrors what the legacy ``dispatch.py`` table did
# via ``DispatchEntry.event_*`` fields.
_KERNEL_SYSCALL_EVENTS: dict[str, dict[str, str]] = {
    # File mutations
    "sys_write": {"event_type": "file_write", "path_attr": "path", "size_key": "bytes_written"},
    "write": {"event_type": "file_write", "path_attr": "path", "size_key": "bytes_written"},
    "sys_unlink": {"event_type": "file_delete", "path_attr": "path"},
    "delete": {"event_type": "file_delete", "path_attr": "path"},
    "sys_rename": {
        "event_type": "file_rename",
        "path_attr": "new_path",
        "old_path_attr": "old_path",
    },
    "rename": {
        "event_type": "file_rename",
        "path_attr": "new_path",
        "old_path_attr": "old_path",
    },
    # Directory mutations
    "sys_mkdir": {"event_type": "dir_create", "path_attr": "path"},
    "mkdir": {"event_type": "dir_create", "path_attr": "path"},
    "sys_rmdir": {"event_type": "dir_delete", "path_attr": "path"},
    "rmdir": {"event_type": "dir_delete", "path_attr": "path"},
}


def _scan_pykernel_syscalls(classes: dict[str, "ClassDef"]) -> list[str]:
    """Return the syscall method names actually present on PyKernel.

    Filters the allowlist down to methods we can verify exist in the
    PyO3 #[pymethods] block — guards against stale allowlist entries
    surviving a kernel-side rename without surfacing as a codegen
    diff.  Returns names in allowlist order so the generated file is
    stable.
    """
    pykernel = classes.get("PyKernel")
    if pykernel is None:
        # Codegen ran before PyKernel was parsed (shouldn't happen
        # in normal flow) — defensively return an empty list rather
        # than blowing up.
        return []
    have = {m.name for m in pykernel.methods}
    return [name for name in _KERNEL_SYSCALL_ALLOWLIST if name in have]


def generate_kernel_syscall_dispatch(classes: dict[str, "ClassDef"]) -> str:
    """Emit the thin syscall dispatch module.

    Caller wires the generated ``dispatch_kernel_syscall`` into the
    gRPC servicer's ``Call`` handler.  See ``KERNEL_DISPATCH_PATH``.
    """
    syscalls = _scan_pykernel_syscalls(classes)
    # All wire-form names the dispatcher recognizes — direct PyKernel
    # method names plus the alias map's keys plus the manual extras
    # below.  ``sys_readdir`` is the canonical NexusFS method name that
    # REMOTE clients dispatch directly through the gRPC ``Call`` channel
    # (see ``factory/_remote.py::_remote_sys_readdir``); ``close`` is a
    # client-lifecycle method some service proxies erroneously dispatch
    # as RPC during teardown — short-circuited to a no-op in
    # ``dispatch_kernel_syscall`` to avoid tearing down the live server
    # filesystem.
    _MANUAL_WIRE_NAMES = ("sys_readdir", "close")
    wire_names = sorted(
        set(syscalls)
        | set(_KERNEL_SYSCALL_ALIASES.keys())
        | set(_EXTRA_KERNEL_SYSCALL_NAMES)
        | set(_MANUAL_WIRE_NAMES)
    )

    names_block = ",\n        ".join(f'"{n}"' for n in wire_names)
    aliases_block = ",\n    ".join(
        f'"{src}": "{dst}"' for src, dst in sorted(_KERNEL_SYSCALL_ALIASES.items())
    )

    # Event metadata table — emitted as Python literal for the
    # generated module.  repr() handles dict→str safely and keeps
    # the keys sorted for deterministic output.
    events_lines = []
    for name in sorted(_KERNEL_SYSCALL_EVENTS.keys()):
        meta = _KERNEL_SYSCALL_EVENTS[name]
        meta_str = "{" + ", ".join(f'"{k}": "{v}"' for k, v in meta.items()) + "}"
        events_lines.append(f'    "{name}": {meta_str}')
    events_block = ",\n".join(events_lines)

    return f'''\
# AUTO-GENERATED by scripts/codegen_kernel_abi.py — DO NOT EDIT
"""Thin gRPC `Call` dispatch for kernel syscalls.

Source of truth: PyKernel ``#[pymethods]`` in
``rust/kernel/src/generated_kernel_abi_pyo3.rs``.  The codegen
scans that file for syscall methods (sys_read, sys_write,
sys_setattr, …) and emits this module so the gRPC servicer can
route calls to the Python ``NexusFS`` wrapper without any
@rpc_expose / dispatch.py / handlers/filesystem.py / params-dataclass
chain.

The dispatcher uses ``inspect.signature`` to filter kwargs against
the matching NexusFS method, so adding a new kernel syscall is just
``_KERNEL_SYSCALL_ALLOWLIST`` + re-run the codegen.  Methods that
take ``**attrs`` (sys_setattr) get arbitrary extras forwarded
unchanged.

Mutation syscalls fire pub/sub events through the gRPC servicer's
``subscription_manager`` (file_write / file_delete / file_rename /
dir_create / dir_delete) — see ``_EVENTS`` for the wire-name →
event-metadata mapping.  Tier 2 wire-shape adapters
(``{{"deleted": True}}`` / ``{{"renamed": True}}`` / ``bytes_written``
merge / ``{{"acquired", "lock_id"}}``) live in
``_apply_result_adapter`` and preserve the legacy ``handle_*``
wrapper response shapes.

Re-generate: python scripts/codegen_kernel_abi.py
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


# Wire-form RPC method names this module handles. The gRPC servicer
# tests membership BEFORE invoking the legacy ``dispatch_method`` path.
KERNEL_SYSCALL_NAMES: frozenset[str] = frozenset({{
        {names_block},
}})


# Wire name → canonical NexusFS method name (see codegen for the why).
_ALIASES: dict[str, str] = {{
    {aliases_block},
}}


# Wire name → event metadata for pub/sub firing on mutation syscalls.
# Mirrors the legacy ``DispatchEntry.event_*`` fields.
_EVENTS: dict[str, dict[str, str]] = {{
{events_block},
}}


def _resolve_method_name(method: str) -> str:
    """Resolve a wire-form RPC name to the NexusFS method to invoke."""
    return _ALIASES.get(method, method)


def _build_kwargs(
    func: Any,
    params: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    """Filter ``params`` to ``func``'s signature; forward extras to **kwargs.

    Methods like ``sys_setattr(path, *, context, **attrs)`` collect
    arbitrary extras through their VAR_KEYWORD param — without explicit
    forwarding, ``inspect.signature`` would drop them.  This is how
    DT_MOUNT's ``entry_type`` / ``zone_id`` / ``backend_name`` reach
    the kernel without each needing a named slot in NexusFS's signature.
    """
    sig = inspect.signature(func)
    has_var_keyword = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )

    kwargs: dict[str, Any] = {{}}
    matched: set[str] = set()
    for name, param in sig.parameters.items():
        if name == "self" or param.kind == inspect.Parameter.VAR_KEYWORD:
            continue
        if name in ("context", "_context"):
            kwargs[name] = context
            matched.add(name)
        elif name in params:
            kwargs[name] = params[name]
            matched.add(name)

    if has_var_keyword:
        for k, v in params.items():
            if k not in matched and k not in ("context", "_context"):
                kwargs[k] = v

    return kwargs


def _apply_result_adapter(
    method: str,
    raw_result: Any,
    params: dict[str, Any],
) -> Any:
    """Re-shape NexusFS results into the wire format Tier 2 callers expect.

    Mirrors the response shapes the legacy ``handle_*`` wrappers
    produced so deleting the legacy chain is a no-op on the wire.

    Path-bearing dicts (sys_stat metadata, sys_readdir entries) get
    their internal ``/zone/<id>/...`` prefix stripped via
    ``unscope_internal_dict`` so RPC callers see user-facing paths.

    ``params`` is the wire-form input dict — required for write
    syscalls so we can compute ``bytes_written`` from the request
    payload (``content`` / ``buf``) when the kernel returns just
    ``None`` or the new metadata.
    """
    from nexus.server.path_utils import (
        unscope_internal_dict,
        unscope_internal_path,
    )

    if method in ("delete", "sys_unlink"):
        return {{"deleted": True}}
    if method in ("rename", "sys_rename"):
        return {{"renamed": True}}
    if method in ("mkdir", "sys_mkdir"):
        return {{"created": True}}
    if method in ("rmdir", "sys_rmdir"):
        return {{"removed": True}}
    if method in ("write", "sys_write"):
        content = params.get("content") or params.get("buf") or b""
        content_len = (
            len(content.encode("utf-8")) if isinstance(content, str) else len(content)
        )
        wire: dict[str, Any] = {{"bytes_written": content_len}}
        if isinstance(raw_result, dict):
            wire.update(raw_result)
        return wire
    if method == "sys_stat":
        # `Kernel::sys_stat` returns the StatResult dict directly
        # (or `None` for missing files).  Match the kernel ABI shape
        # so consumers can test `result is None` for not-found.  The
        # legacy `{{"metadata": ...}}` wrap was for a Tier 2 caller
        # that has been deleted; the `unscope_internal_dict` call is
        # still applied to strip internal `path` keys from the dict.
        if isinstance(raw_result, dict):
            return unscope_internal_dict(raw_result, ["path"])
        return raw_result
    if method in ("access", "exists"):
        return {{"exists": bool(raw_result)}}
    if method == "is_directory":
        return {{"is_directory": bool(raw_result)}}
    if method == "sys_unlock":
        return {{"released": bool(raw_result)}}
    if method == "sys_lock":
        # Tier 1 contract: bare `lock_id` string when granted, `None`
        # on contention.  Matches the kernel ABI's `Option<String>`
        # exactly; consumers test `result is None` for contention.
        # The legacy Tier 2 dict wrapper was removed alongside
        # `lock_acquire` in commit 231620c3c.
        return raw_result
    if method == "lock_acquire":
        return {{"acquired": raw_result is not None, "lock_id": raw_result}}
    if method in ("sys_readdir", "list"):
        # PaginatedResult (from limit kwarg) → wire dict; bare list →
        # wire dict with has_more=False / next_cursor=None.
        if hasattr(raw_result, "to_dict"):
            paginated = raw_result.to_dict()
            items = [
                unscope_internal_dict(f, ["path", "virtual_path"])
                if isinstance(f, dict)
                else unscope_internal_path(f)
                for f in paginated["items"]
            ]
            return {{
                "files": items,
                "next_cursor": paginated["next_cursor"],
                "has_more": paginated["has_more"],
                "total_count": paginated.get("total_count"),
            }}
        raw_entries = raw_result if isinstance(raw_result, list) else []
        entries = [
            unscope_internal_dict(f, ["path", "virtual_path"])
            if isinstance(f, dict)
            else unscope_internal_path(f)
            for f in raw_entries
        ]
        return {{"files": entries, "has_more": False, "next_cursor": None}}
    return raw_result


async def _fire_event(
    subscription_manager: Any,
    method: str,
    params: dict[str, Any],
    result: Any,
    context: Any,
) -> None:
    """Fire pub/sub event for a mutation syscall (best-effort, non-blocking).

    No-op when the wire method has no entry in ``_EVENTS`` or
    ``subscription_manager`` is None.  Pulls path / old_path / size
    from the params dict + result via ``_EVENTS`` metadata.
    """
    meta = _EVENTS.get(method)
    if meta is None or subscription_manager is None:
        return

    try:
        zone_id = getattr(context, "zone_id", None) or ROOT_ZONE_ID
        path = params.get(meta.get("path_attr", "path"))
        if not path:
            return
        data: dict[str, Any] = {{"file_path": path}}
        old_path_attr = meta.get("old_path_attr")
        if old_path_attr:
            old_path = params.get(old_path_attr)
            if old_path:
                data["old_path"] = old_path
        size_key = meta.get("size_key")
        if size_key and isinstance(result, dict) and size_key in result:
            data["size"] = result[size_key]
        await subscription_manager.broadcast(meta["event_type"], data, zone_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[kernel-syscall-dispatch] event firing failed for %s on %s: %s",
            meta.get("event_type"),
            method,
            exc,
        )


async def _occ_write_dispatch(
    nexus_fs: Any,
    params: dict[str, Any],
    context: Any,
) -> Any:
    """Special-case write with OCC (``if_match`` / ``if_none_match``).

    Mirrors what the legacy ``handle_write`` wrapper did — when the
    RPC carries OCC kwargs and ``force`` is not set, route through
    ``nexus.lib.occ.occ_write`` so the CAS check happens at the RPC
    layer.  Otherwise fall through to plain ``NexusFS.write``.

    Returned bytes_written count is always derived from the content
    payload (see ``_apply_result_adapter``).
    """
    from nexus.lib.occ import occ_write

    content = params.get("content") or params.get("buf") or b""
    if isinstance(content, str):
        content = content.encode("utf-8")
    offset = int(params.get("offset", 0) or 0)
    if_match = params.get("if_match") or None
    if_none_match = bool(params.get("if_none_match"))
    force = bool(params.get("force"))

    if (if_match or if_none_match) and not force:
        return await occ_write(
            nexus_fs,
            params["path"],
            content,
            context=context,
            if_match=if_match,
            if_none_match=if_none_match,
            offset=offset,
        )
    # #4005 round-2: NexusFS.write is sync — offload like dispatch_kernel_syscall.
    return await asyncio.to_thread(
        nexus_fs.write, params["path"], content, context=context, offset=offset
    )


def _apply_pre_call_defaults(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Apply legacy-conservative defaults to wire params (Codex review of #3701).

    ``NexusFS.mkdir`` defaults to ``parents=True, exist_ok=True``
    (mkdir -p) and ``NexusFS.rmdir`` defaults to ``recursive=True``
    (rm -rf), but the legacy ``mkdir`` / ``rmdir`` / ``sys_mkdir``
    / ``sys_rmdir`` RPC aliases have always been conservative so
    legacy clients sending only ``{{"path": "/foo"}}`` get a plain
    mkdir that errors on missing parents / existing paths and a
    non-recursive rmdir.  Dropping the override would silently turn
    safe legacy calls into destructive recursive deletes / silent
    mkdir-p — a real behavioral regression flagged by Codex.

    Returns a (possibly defaulted) shallow copy of params; the
    caller passes the result to ``_build_kwargs``.  Non-syscall
    methods get back the same dict.
    """
    if method in ("mkdir", "sys_mkdir"):
        out = dict(params)
        out.setdefault("parents", False)
        out.setdefault("exist_ok", False)
        return out
    if method in ("rmdir", "sys_rmdir"):
        out = dict(params)
        out.setdefault("recursive", False)
        return out
    return params


async def dispatch_kernel_syscall(
    nexus_fs: Any,
    method: str,
    params: dict[str, Any],
    context: Any,
    *,
    subscription_manager: Any = None,
) -> Any:
    """Dispatch a kernel syscall RPC to the matching ``NexusFS`` method.

    * Resolves wire alias to canonical NexusFS method via ``_ALIASES``.
    * Filters params to the method's signature with ``_build_kwargs``;
      ``**attrs`` methods (sys_setattr) get extras forwarded.
    * Special-cases ``write`` / ``sys_write`` for OCC (if_match /
      if_none_match) — routes through ``occ_write`` when those kwargs
      are present.
    * Applies legacy-conservative ``mkdir`` / ``rmdir`` defaults via
      ``_apply_pre_call_defaults`` (Codex review of #3701).
    * Reshapes the result via ``_apply_result_adapter`` so legacy
      Tier 2 wire shapes are preserved (``{{"deleted": True}}`` etc.).
    * Fires the matching pub/sub event via ``_fire_event`` when the
      caller passes a ``subscription_manager``.

    Returns the (possibly adapted) result; gRPC servicer encodes it.
    """
    # ``close`` is a client-side lifecycle method that some REMOTE service
    # proxies erroneously dispatch through the RPC transport during teardown.
    # Calling ``NexusFS.close`` server-side would tear down the live server
    # filesystem — short-circuit to a no-op so the wire call succeeds without
    # affecting server state.
    if method == "close":
        return {{}}

    params = _apply_pre_call_defaults(method, params)

    if method in ("write", "sys_write"):
        raw_result = await _occ_write_dispatch(nexus_fs, params, context)
    else:
        canonical = _resolve_method_name(method)
        func = getattr(nexus_fs, canonical, None)
        if func is None:
            raise AttributeError(
                f"NexusFS has no method {{canonical!r}} (from RPC method {{method!r}})"
            )

        kwargs = _build_kwargs(func, params, context)

        if asyncio.iscoroutinefunction(func):
            raw_result = await func(**kwargs)
        else:
            # #4005 round-2: NexusFS sys_* methods are sync and may do
            # blocking I/O (DB hits, large reads). Calling them inline
            # would park the asyncio loop — same DoS class that justified
            # excluding sys_watch. Offload to the default thread pool.
            raw_result = await asyncio.to_thread(func, **kwargs)

    result = _apply_result_adapter(method, raw_result, params)

    await _fire_event(subscription_manager, method, params, result, context)

    return result
'''


# ── Main ──────────────────────────────────────────────────────────


def _ruff_cmd() -> list[str] | None:
    """Return a ruff command usable from plain and pre-commit hook envs."""
    ruff = shutil.which("ruff")
    if ruff:
        return [ruff]
    uv = shutil.which("uv")
    if not uv:
        uv_path = Path.home() / ".local" / "bin" / "uv"
        if uv_path.exists():
            uv = str(uv_path)
    if uv:
        return [uv, "run", "--extra", "dev", "ruff"]
    return None


def main() -> int:
    check_mode = "--check" in sys.argv

    module_functions, classes, class_order, traits, all_names = collect_all()

    stubs_content = generate_stubs(module_functions, classes, class_order)
    protocols_content = generate_protocols(traits)
    exports_content = generate_exports(all_names)
    api_groups_content = generate_api_groups(classes)
    pyo3_content = generate_pyo3_rs(traits)
    kernel_dispatch_content = generate_kernel_syscall_dispatch(classes)
    outputs: list[tuple[Path, str]] = [
        (STUBS_PATH, stubs_content),
        (PROTOCOLS_PATH, protocols_content),
        (EXPORTS_PATH, exports_content),
        (API_GROUPS_PATH, api_groups_content),
        (GENERATED_PYO3_PATH, pyo3_content),
        (KERNEL_DISPATCH_PATH, kernel_dispatch_content),
    ]

    if check_mode:
        # For Python files, ruff-format the expected content before comparing
        import subprocess
        import tempfile

        ruff = _ruff_cmd()

        rustfmt = shutil.which("rustfmt")

        def _ruff_format(content: str, suffix: str) -> str:
            # mode="wb" + write_bytes parity: text-mode `mode="w"`
            # translates `\n` → `\r\n` on Windows under some Python
            # interpreters (notably venv python.exe vs WindowsApps
            # python3) which makes ruff format the temp file with
            # CRLF input — its output then differs from how main()
            # formats LF on-disk.  Force LF byte-exactness instead.
            if ruff and suffix in (".py", ".pyi"):
                with tempfile.NamedTemporaryFile(mode="wb", suffix=suffix, delete=False) as f:
                    f.write(content.encode("utf-8"))
                    f.flush()
                    subprocess.run(
                        [
                            *ruff,
                            "format",
                            "--config",
                            str(ROOT / "pyproject.toml"),
                            f.name,
                        ],
                        capture_output=True,
                    )
                    return Path(f.name).read_bytes().decode("utf-8")
            if rustfmt and suffix == ".rs":
                with tempfile.NamedTemporaryFile(mode="wb", suffix=suffix, delete=False) as f:
                    f.write(content.encode("utf-8"))
                    f.flush()
                    subprocess.run([rustfmt, f.name], capture_output=True)
                    return Path(f.name).read_bytes().decode("utf-8")
            return content

        ok = True
        for path, expected in outputs:
            if not path.exists():
                print(f"MISSING: {path.relative_to(ROOT)}")
                ok = False
                continue
            expected = _ruff_format(expected, path.suffix)
            actual = path.read_text()
            if actual != expected:
                print(f"STALE: {path.relative_to(ROOT)}")
                # Show first differing line
                sentinel = object()
                for i, (a, e) in enumerate(
                    zip_longest(
                        actual.splitlines(),
                        expected.splitlines(),
                        fillvalue=sentinel,
                    ),
                    1,
                ):
                    if a != e:
                        print(f"  line {i}:")
                        actual_line = "<missing>" if a is sentinel else repr(a)
                        expected_line = "<missing>" if e is sentinel else repr(e)
                        print(f"    actual:   {actual_line}")
                        print(f"    expected: {expected_line}")
                        break
                ok = False
            else:
                print(f"OK: {path.relative_to(ROOT)}")
        return 0 if ok else 1
    else:
        py_files: list[Path] = []
        for path, content in outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Force LF line endings regardless of platform so Windows users
            # don't generate CRLF-polluted commits (git core.autocrlf=false
            # here means text-mode write would leak '\r\n' into the tree).
            path.write_bytes(content.encode("utf-8"))
            rel = path.relative_to(ROOT)
            print(f"wrote {rel}")
            if path.suffix in (".py", ".pyi"):
                py_files.append(path)
        # Auto-format generated Python files so codegen --check matches ruff format
        if py_files:
            ruff = _ruff_cmd()
            if ruff:
                import subprocess

                subprocess.run(
                    [
                        *ruff,
                        "format",
                        "--config",
                        str(ROOT / "pyproject.toml"),
                        *[str(p) for p in py_files],
                    ],
                    capture_output=True,
                )
        # Auto-format generated Rust files so codegen --check matches cargo fmt
        rs_files = [p for p, _ in outputs if p.suffix == ".rs"]
        if rs_files:
            import subprocess

            rustfmt = shutil.which("rustfmt")
            if rustfmt:
                subprocess.run(
                    [rustfmt, *[str(p) for p in rs_files]],
                    capture_output=True,
                )
        return 0


if __name__ == "__main__":
    sys.exit(main())
