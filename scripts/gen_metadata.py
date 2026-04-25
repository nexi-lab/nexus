#!/usr/bin/env python3
"""Generate Python code from proto/nexus/core/metadata.proto.

SSOT: proto/nexus/core/metadata.proto is the single source of truth
for FileMetadata fields. This script generates:

  - src/nexus/core/metadata_pb2.py         (protobuf stubs via grpc_tools.protoc)
  - src/nexus/core/metadata.py             (FileMetadata data class)
  - src/nexus/core/metastore.py            (MetastoreABC — hand-maintained, not generated)
  - src/nexus/core/_compact_generated.py   (CompactFileMetadata + interning)

Usage:
    python scripts/gen_metadata.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Resolve paths relative to repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
PROTO_PATH = REPO_ROOT / "proto" / "nexus" / "core" / "metadata.proto"
METADATA_OUT = REPO_ROOT / "src" / "nexus" / "contracts" / "metadata.py"
METASTORE_OUT = REPO_ROOT / "src" / "nexus" / "core" / "metastore.py"
COMPACT_OUT = REPO_ROOT / "src" / "nexus" / "core" / "_compact_generated.py"
MAPPER_OUT = REPO_ROOT / "src" / "nexus" / "storage" / "_metadata_mapper_generated.py"

# --- Generated class names (SSOT) ---
# Canonical names exported by each generated module.
# The SSOT audit checks all downstream imports match these.
GENERATED_NAMES: dict[str, set[str]] = {
    "metadata": {
        "FileMetadata",
        "DT_REG",
        "DT_DIR",
        "DT_MOUNT",
        "DT_PIPE",
        "DT_STREAM",
        "DT_EXTERNAL_STORAGE",
    },
    "metastore": {
        "MetastoreABC",
    },
    "_compact_generated": {"CompactFileMetadata", "get_intern_pool_stats", "clear_intern_pool"},
    "_metadata_mapper_generated": {"MetadataMapper"},
}

# --- One-time migration: old → new name ---
# When a generated class is renamed, add old→new here. The generator
# will update all downstream imports in src/ and tests/, then DELETE
# the entry. This is NOT backward compatibility — no aliases are kept.
RENAMES: dict[str, str] = {}

# --- Proto field configuration ---
# When you add a field to metadata.proto, update these mappings.

# Proto type -> Python type
PROTO_TYPE_MAP: dict[str, str] = {
    "string": "str",
    "int64": "int",
    "int32": "int",
    "double": "float",
    "bool": "bool",
    "DirEntryType": "int",  # Enum stored as int in Python
}

# Fields where Python uses datetime | None instead of str
DATETIME_FIELDS: set[str] = {"created_at", "modified_at"}

# String fields that are nullable (str | None, default None)
NULLABLE_STRING_FIELDS: set[str] = {
    "etag",
    "mime_type",
    "zone_id",
    "created_by",
    "owner_id",
    "target_zone_id",
    "last_writer_address",
}

# Non-default defaults
FIELD_DEFAULTS: dict[str, str] = {
    "version": "1",
    "entry_type": "0",
    "ttl_seconds": "0.0",
}

# String fields that get interned in CompactFileMetadata
INTERNED_FIELDS: list[str] = [
    "path",
    "etag",
    "mime_type",
    "zone_id",
    "created_by",
    "owner_id",
    "target_zone_id",
    "last_writer_address",
]

# Compact field name mapping
COMPACT_FIELD_NAMES: dict[str, str] = {
    "path": "path_id",
    "etag": "etag_id",
    "mime_type": "mime_type_id",
    "zone_id": "zone_id_intern",
    "created_by": "created_by_id",
    "owner_id": "owner_id_intern",
    "target_zone_id": "target_zone_id_intern",
    "last_writer_address": "last_writer_address_id",
}

# from_proto fallback: when a proto field is empty, use another field's value
FROM_PROTO_FALLBACKS: dict[str, str] = {}

# Fields stored directly (not interned) in CompactFileMetadata
DIRECT_COMPACT_FIELDS: dict[str, str] = {
    "size": "int",
    "version": "int",
    "entry_type": "int",
}


# --- Proto parser ---


def parse_proto_enums(proto_path: Path) -> dict[str, list[tuple[str, int]]]:
    """Parse enum definitions from proto file.

    Returns dict mapping enum name to list of (value_name, value_number).
    Example: {"DirEntryType": [("DT_REG", 0), ("DT_DIR", 1), ("DT_MOUNT", 2)]}
    """
    content = proto_path.read_text(encoding="utf-8")
    enums: dict[str, list[tuple[str, int]]] = {}

    for m in re.finditer(r"enum\s+(\w+)\s*\{(.*?)\}", content, re.DOTALL):
        enum_name = m.group(1)
        body = m.group(2)
        values: list[tuple[str, int]] = []
        for vm in re.finditer(r"(\w+)\s*=\s*(\d+)\s*;", body):
            values.append((vm.group(1), int(vm.group(2))))
        enums[enum_name] = values

    return enums


def parse_proto_fields(proto_path: Path) -> list[dict[str, str]]:
    """Parse FileMetadata fields from proto file.

    Returns list of dicts with keys: name, type, number, comment.
    """
    content = proto_path.read_text(encoding="utf-8")

    match = re.search(r"message\s+FileMetadata\s*\{(.*?)\}", content, re.DOTALL)
    if not match:
        print("ERROR: Could not find 'message FileMetadata' in proto file", file=sys.stderr)
        sys.exit(1)

    body = match.group(1)
    fields = []

    field_re = re.compile(r"^\s*(\w+)\s+(\w+)\s*=\s*(\d+)\s*;(?:\s*//\s*(.*))?$", re.MULTILINE)

    lines = body.split("\n")
    prev_comment = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("//"):
            prev_comment = stripped.lstrip("/ ").strip()
            continue

        m = field_re.match(line)
        if m:
            inline_comment = m.group(4) or ""
            comment = inline_comment or prev_comment
            fields.append(
                {
                    "type": m.group(1),
                    "name": m.group(2),
                    "number": m.group(3),
                    "comment": comment,
                }
            )
            prev_comment = ""
        elif stripped:
            prev_comment = ""

    return fields


# --- Code generators ---


def python_type_for(field: dict[str, str]) -> str:
    """Get Python type annotation for a proto field."""
    name = field["name"]
    if name in DATETIME_FIELDS:
        return "datetime | None"
    base_type = PROTO_TYPE_MAP.get(field["type"], field["type"])
    if name in NULLABLE_STRING_FIELDS:
        return f"{base_type} | None"
    return base_type


def python_default_for(field: dict[str, str]) -> str | None:
    """Get Python default value, or None if no default."""
    name = field["name"]
    if name in FIELD_DEFAULTS:
        return FIELD_DEFAULTS[name]
    if name in DATETIME_FIELDS or name in NULLABLE_STRING_FIELDS:
        return "None"
    return None


def _enum_common_prefix(values: list[tuple[str, int]]) -> str:
    """Find common prefix of enum value names ending with '_'.

    Example: [("DT_REG", 0), ("DT_DIR", 1)] -> "DT_"
    """
    if not values:
        return ""
    names = [v[0] for v in values]
    prefix = names[0]
    for name in names[1:]:
        while not name.startswith(prefix):
            prefix = prefix[:-1]
    # Trim to last '_' boundary
    idx = prefix.rfind("_")
    return prefix[: idx + 1] if idx >= 0 else ""


def _generate_enum_constants(enums: dict[str, list[tuple[str, int]]]) -> str:
    """Generate Python constants from proto enums.

    Example output:
        # DirEntryType (from proto/nexus/core/metadata.proto)
        DT_REG = 0
        DT_DIR = 1
        DT_MOUNT = 2
    """
    blocks = []
    for enum_name, values in enums.items():
        lines = [f"# {enum_name} (from proto/nexus/core/metadata.proto)"]
        for vname, vnum in values:
            lines.append(f"{vname} = {vnum}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _generate_enum_properties(
    fields: list[dict[str, str]],
    enums: dict[str, list[tuple[str, int]]],
) -> str:
    """Generate @property methods for enum-typed fields.

    For field 'entry_type' of type 'DirEntryType' with values
    DT_REG=0, DT_DIR=1, DT_MOUNT=2, generates:
        @property
        def is_reg(self) -> bool: return self.entry_type == 0
        @property
        def is_dir(self) -> bool: return self.entry_type == 1
        @property
        def is_mount(self) -> bool: return self.entry_type == 2
    """
    lines = []
    for f in fields:
        enum_values = enums.get(f["type"])
        if not enum_values:
            continue
        field_name = f["name"]
        prefix = _enum_common_prefix(enum_values)
        for vname, vnum in enum_values:
            prop_name = "is_" + vname.removeprefix(prefix).lower()
            lines.append("    @property")
            lines.append(f"    def {prop_name}(self) -> bool:")
            lines.append(f"        return self.{field_name} == {vnum}")
            lines.append("")
    return "\n".join(lines)


def _generate_to_dict(fields: list[dict[str, str]]) -> str:
    """Generate to_dict() dict entries for FileMetadata.

    Datetime fields are serialized as ISO 8601 strings.
    All other fields are passed through directly.
    """
    lines = []
    for f in fields:
        name = f["name"]
        if name in DATETIME_FIELDS:
            lines.append(f'            "{name}": self.{name}.isoformat() if self.{name} else None,')
        else:
            lines.append(f'            "{name}": self.{name},')
    return "\n".join(lines)


def generate_metadata_py(
    fields: list[dict[str, str]],
    enums: dict[str, list[tuple[str, int]]] | None = None,
) -> str:
    """Generate _metadata_generated.py content."""
    enums = enums or {}

    # Build field lines
    field_lines = []
    for f in fields:
        py_type = python_type_for(f)
        default = python_default_for(f)

        if default is not None:
            line = f"    {f['name']}: {py_type} = {default}"
        else:
            line = f"    {f['name']}: {py_type}"

        field_lines.append(line)

    fields_block = "\n".join(field_lines)

    # Build enum constants, properties, and to_dict
    enum_constants = _generate_enum_constants(enums)
    enum_constants_block = f"\n\n{enum_constants}\n" if enum_constants else ""
    enum_properties = _generate_enum_properties(fields, enums)
    enum_properties_block = f"\n{enum_properties}" if enum_properties else ""
    to_dict_block = _generate_to_dict(fields)

    return f'''\
"""Auto-generated from proto/nexus/core/metadata.proto - DO NOT EDIT.

This module is generated by: python scripts/gen_metadata.py
SSOT: proto/nexus/core/metadata.proto

To modify FileMetadata:
  1. Edit proto/nexus/core/metadata.proto
  2. Run: python scripts/gen_metadata.py
  3. Never edit this file directly!

Contains:
  - FileMetadata: Core file metadata dataclass
  - DT_REG, DT_DIR, DT_MOUNT, DT_PIPE, DT_STREAM, DT_EXTERNAL_STORAGE: Directory entry type constants
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core._compact_generated import CompactFileMetadata
{enum_constants_block}

@dataclass(slots=True)
class FileMetadata:
    """File metadata information.

    Generated from: proto/nexus/core/metadata.proto
    """

{fields_block}
{enum_properties_block}
    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict.

        Handles datetime -> ISO 8601 string conversion.
        Generated from proto field definitions (SSOT).
        """
        return {{
{to_dict_block}
        }}

    def validate(self) -> None:
        """Validate file metadata before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.contracts.exceptions import ValidationError

        if not self.path:
            raise ValidationError("path is required")

        if not self.path.startswith("/"):
            raise ValidationError(f"path must start with '/', got {{self.path!r}}", path=self.path)

        if "\\x00" in self.path:
            raise ValidationError("path contains null bytes", path=self.path)

        # DT_PIPE/DT_STREAM inodes: in-memory buffers, no backend storage required
        if self.entry_type in (3, 4):  # DT_PIPE, DT_STREAM
            return

        if self.size < 0:
            raise ValidationError(f"size cannot be negative, got {{self.size}}", path=self.path)

        if self.version < 1:
            raise ValidationError(f"version must be >= 1, got {{self.version}}", path=self.path)

    def to_compact(self) -> CompactFileMetadata:
        """Convert to memory-efficient CompactFileMetadata.

        Uses string interning to deduplicate path/hash strings across instances.
        Reduces memory from ~200-300 bytes to ~64-100 bytes per instance.

        Returns:
            CompactFileMetadata with interned strings and packed fields
        """
        from nexus.core._compact_generated import CompactFileMetadata

        return CompactFileMetadata.from_file_metadata(self)

    @classmethod
    def from_compact(cls, compact: CompactFileMetadata) -> FileMetadata:
        """Create FileMetadata from CompactFileMetadata.

        Resolves interned string IDs back to full strings.

        Args:
            compact: CompactFileMetadata instance

        Returns:
            Full FileMetadata object
        """
        return compact.to_file_metadata()
'''


def generate_compact_py(fields: list[dict[str, str]]) -> str:
    """Generate _compact_generated.py content."""
    # Build CompactFileMetadata field declarations
    compact_field_lines = []
    for f in fields:
        name = f["name"]
        if name in COMPACT_FIELD_NAMES:
            cname = COMPACT_FIELD_NAMES[name]
            compact_field_lines.append(f"    {cname}: int")
        elif name in DATETIME_FIELDS:
            compact_field_lines.append(f"    {name}: str | None")
        elif name in DIRECT_COMPACT_FIELDS:
            compact_field_lines.append(f"    {name}: {DIRECT_COMPACT_FIELDS[name]}")
    compact_fields_block = "\n".join(compact_field_lines)

    # Build from_file_metadata keyword args
    ctor_args = []
    for f in fields:
        name = f["name"]
        if name in COMPACT_FIELD_NAMES:
            cname = COMPACT_FIELD_NAMES[name]
            ctor_args.append(f"            {cname}=_intern(m.{name}),")
        elif name in DATETIME_FIELDS:
            ctor_args.append(f"            {name}=m.{name}.isoformat() if m.{name} else None,")
        elif name in DIRECT_COMPACT_FIELDS:
            ctor_args.append(f"            {name}=m.{name},")
    ctor_block = "\n".join(ctor_args)

    # Build to_file_metadata keyword args
    # Required string fields use _resolve_required() for type safety
    required_string_fields = {n for n in COMPACT_FIELD_NAMES if n not in NULLABLE_STRING_FIELDS}
    fm_args = []
    for f in fields:
        name = f["name"]
        if name in COMPACT_FIELD_NAMES:
            cname = COMPACT_FIELD_NAMES[name]
            if name in required_string_fields:
                fm_args.append(f"            {name}=_resolve_required(self.{cname}),")
            else:
                fm_args.append(f"            {name}=_resolve(self.{cname}),")
        elif name in DATETIME_FIELDS:
            fm_args.append(
                f"            {name}=datetime.fromisoformat(self.{name}) if self.{name} else None,"
            )
        elif name in DIRECT_COMPACT_FIELDS:
            fm_args.append(f"            {name}=self.{name},")
    fm_block = "\n".join(fm_args)

    return f'''\
"""Auto-generated from proto/nexus/core/metadata.proto - DO NOT EDIT.

This module is generated by: python scripts/gen_metadata.py
SSOT: proto/nexus/core/metadata.proto

Compact file metadata for memory-efficient storage at scale.

String fields are stored as integer IDs (4 bytes each) instead of
full string objects. This reduces memory from ~200-300 bytes to
~64-100 bytes per file at 1M+ file scale.

Timestamps are stored as ISO 8601 strings to preserve precision
and timezone information across serialization boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.contracts.metadata import FileMetadata

# --- String interning ---
# Single global pool: string -> int ID, and reverse lookup.
# All string fields share one pool for simplicity.

_STRING_POOL: dict[str, int] = {{}}
_STRING_POOL_REVERSE: dict[int, str] = {{}}
_NEXT_ID: int = 0


def _intern(s: str | None) -> int:
    """Intern a string and return its ID. Returns -1 for None."""
    global _NEXT_ID
    if s is None:
        return -1
    if s not in _STRING_POOL:
        _STRING_POOL[s] = _NEXT_ID
        _STRING_POOL_REVERSE[_NEXT_ID] = s
        _NEXT_ID += 1
    return _STRING_POOL[s]


def _resolve(id: int) -> str | None:
    """Resolve a string ID back to its value. Returns None for -1."""
    if id == -1:
        return None
    return _STRING_POOL_REVERSE.get(id)


def _resolve_required(id: int) -> str:
    """Resolve a required string field. Raises if not found."""
    result = _STRING_POOL_REVERSE.get(id)
    if result is None:
        raise ValueError(f"Interned string ID {{id}} not found in pool")
    return result


@dataclass(frozen=True)
class CompactFileMetadata:
    """Memory-optimized FileMetadata using string interning.

    Generated from: proto/nexus/core/metadata.proto

    String fields are stored as integer IDs (4 bytes each) instead of
    full string objects. This reduces per-instance memory significantly.
    """

{compact_fields_block}

    @classmethod
    def from_file_metadata(cls, m: FileMetadata) -> CompactFileMetadata:
        """Create CompactFileMetadata from FileMetadata."""
        return cls(
{ctor_block}
        )

    def to_file_metadata(self) -> FileMetadata:
        """Convert back to FileMetadata."""
        from nexus.contracts.metadata import FileMetadata

        return FileMetadata(
{fm_block}
        )


def get_intern_pool_stats() -> dict[str, int]:
    """Get string interning pool statistics."""
    return {{
        "count": len(_STRING_POOL),
        "memory_estimate": sum(len(s) for s in _STRING_POOL) + len(_STRING_POOL) * 100,
    }}


def clear_intern_pool() -> None:
    """Clear the intern pool. Use only for testing."""
    global _NEXT_ID
    _STRING_POOL.clear()
    _STRING_POOL_REVERSE.clear()
    _NEXT_ID = 0
'''


def _field_category(field: dict[str, str]) -> str:
    """Classify a proto field for mapper code generation.

    Returns one of: 'datetime', 'nullable_string', 'int', 'required_string'.
    """
    name = field["name"]
    if name in DATETIME_FIELDS:
        return "datetime"
    if name in NULLABLE_STRING_FIELDS:
        return "nullable_string"
    proto_type = field["type"]
    if proto_type in ("int64", "int32"):
        return "int"
    if proto_type in PROTO_TYPE_MAP and PROTO_TYPE_MAP[proto_type] == "int":
        return "enum"
    return "required_string"


def generate_mapper_py(fields: list[dict[str, str]]) -> str:
    """Generate _metadata_mapper_generated.py content.

    Produces MetadataMapper with to_proto/from_proto/to_json/from_json
    derived from the proto field list. SQL methods are included verbatim
    in the template (they use a different column name mapping).
    """
    # --- Build to_proto keyword args ---
    to_proto_lines = []
    for f in fields:
        name = f["name"]
        cat = _field_category(f)
        if cat == "datetime":
            to_proto_lines.append(
                f'            {name}=metadata.{name}.isoformat() if metadata.{name} else "",'
            )
        elif cat == "nullable_string":
            to_proto_lines.append(f'            {name}=metadata.{name} or "",')
        elif cat == "enum":
            enum_type = f["type"]
            to_proto_lines.append(
                f"            {name}=metadata_pb2.{enum_type}.Name(metadata.{name}),"
            )
        elif cat == "int":
            to_proto_lines.append(f"            {name}=metadata.{name},")
        else:  # required_string
            if name in FROM_PROTO_FALLBACKS:
                to_proto_lines.append(f'            {name}=metadata.{name} or "",')
            else:
                to_proto_lines.append(f"            {name}=metadata.{name},")
    to_proto_block = "\n".join(to_proto_lines)

    # --- Build from_proto keyword args ---
    from_proto_lines = []
    for f in fields:
        name = f["name"]
        cat = _field_category(f)
        if cat == "datetime":
            # Handled separately via local variables
            from_proto_lines.append(f"            {name}={name},")
        elif cat == "nullable_string":
            from_proto_lines.append(f"            {name}=proto.{name} or None,")
        elif cat == "int":
            from_proto_lines.append(f"            {name}=proto.{name},")
        else:  # required_string
            fallback = FROM_PROTO_FALLBACKS.get(name)
            if fallback:
                from_proto_lines.append(f"            {name}=proto.{name} or {fallback},")
            else:
                from_proto_lines.append(f"            {name}=proto.{name},")
    from_proto_block = "\n".join(from_proto_lines)

    # --- Build datetime parsing block for from_proto ---
    datetime_parse_lines = []
    for name in sorted(DATETIME_FIELDS):
        datetime_parse_lines.append(f"        {name} = None")
        datetime_parse_lines.append(f"        if proto.{name}:")
        datetime_parse_lines.append("            with suppress(ValueError):")
        datetime_parse_lines.append(
            f"                {name} = datetime.fromisoformat(proto.{name})"
        )
    datetime_parse_block = "\n".join(datetime_parse_lines)

    # --- Build to_json dict entries ---
    to_json_lines = []
    for f in fields:
        name = f["name"]
        cat = _field_category(f)
        if cat == "datetime":
            to_json_lines.append(
                f'            "{name}": metadata.{name}.isoformat() if metadata.{name} else None,'
            )
        else:
            to_json_lines.append(f'            "{name}": metadata.{name},')
    to_json_block = "\n".join(to_json_lines)

    # --- Build known field names set ---
    # Multi-line layout matches ruff format output so the generated
    # file passes `ruff format --check` without a post-codegen pass.
    known_fields = "\n".join(f'        "{f["name"]}",' for f in fields)
    known_fields = "\n" + known_fields + "\n    "

    return f'''\
"""Auto-generated from proto/nexus/core/metadata.proto - DO NOT EDIT.

This module is generated by: python scripts/gen_metadata.py
SSOT: proto/nexus/core/metadata.proto

Central metadata mapping between FileMetadata and serialization formats.
Proto/JSON methods are auto-generated. SQL methods are manual (different schema).
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID

# Known FileMetadata field names (generated from proto).
# Used by from_json() to strip unknown keys from external dicts.
_KNOWN_FIELDS: frozenset[str] = frozenset(
    {{{known_fields}}}
)

if TYPE_CHECKING:
    from nexus.contracts.metadata import FileMetadata

logger = logging.getLogger(__name__)


def _to_naive(dt: datetime | None) -> datetime | None:
    """Strip timezone from datetime (SQLite stores naive UTC)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _utcnow_naive() -> datetime:
    """Return current UTC time as naive datetime (for SQLite compat)."""
    from datetime import UTC

    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Field name mapping: proto field -> SQLAlchemy column (manual, not generated)
# ---------------------------------------------------------------------------

PROTO_TO_SQL: dict[str, str | None] = {{
    "path": "virtual_path",
    "size": "size_bytes",
    "etag": "content_hash",
    "mime_type": "file_type",
    "created_at": "created_at",
    "modified_at": "updated_at",
    "version": "current_version",
    "zone_id": "zone_id",
    "created_by": None,  # TODO(#1246): Add to FilePathModel
    "entry_type": None,  # TODO(#1246): Add to FilePathModel
    "target_zone_id": None,  # TODO(#1246): Add to FilePathModel
    "owner_id": "posix_uid",
    "last_writer_address": None,  # SQL backend (FilePathModel) doesn't currently persist last writer; add a column when needed.
}}


class MetadataMapper:
    """Centralized mapping between FileMetadata and other representations.

    Proto/JSON methods are auto-generated from proto field definitions.
    SQL methods are manual (different column name mapping).
    """

    # -- Proto serialization (GENERATED) ------------------------------------

    @staticmethod
    def to_proto(metadata: FileMetadata) -> Any:
        """Convert FileMetadata dataclass to protobuf message."""
        from nexus.core import metadata_pb2

        return metadata_pb2.FileMetadata(
{to_proto_block}
        )

    @staticmethod
    def from_proto(proto: Any) -> FileMetadata:
        """Convert protobuf message to FileMetadata dataclass."""
        from nexus.contracts.metadata import FileMetadata

{datetime_parse_block}

        return FileMetadata(
{from_proto_block}
        )

    # -- JSON serialization (GENERATED) -------------------------------------

    @staticmethod
    def to_json(metadata: FileMetadata) -> dict[str, Any]:
        """Convert FileMetadata to JSON-serializable dict."""
        return {{
{to_json_block}
        }}

    @staticmethod
    def from_json(obj: dict[str, Any]) -> FileMetadata:
        """Convert JSON dict to FileMetadata dataclass."""
        from nexus.contracts.metadata import FileMetadata

        # Migration: convert legacy is_directory -> entry_type
        if "is_directory" in obj:
            is_dir = obj.pop("is_directory")
            if "entry_type" not in obj:
                obj["entry_type"] = 1 if is_dir else 0

        # Strip unknown keys (forward compatibility with older/newer proto versions)
        obj = {{k: v for k, v in obj.items() if k in _KNOWN_FIELDS}}

        if obj.get("created_at"):
            obj["created_at"] = datetime.fromisoformat(obj["created_at"])
        if obj.get("modified_at"):
            obj["modified_at"] = datetime.fromisoformat(obj["modified_at"])
        return FileMetadata(**obj)

    # -- SQLAlchemy column values (MANUAL — different schema) ---------------

    @staticmethod
    def to_file_path_values(
        metadata: FileMetadata,
        *,
        include_version: bool = True,
    ) -> dict[str, Any]:
        """Convert FileMetadata to dict of FilePathModel column values.

        Keys are FilePathModel column names (not proto field names).
        """
        values: dict[str, Any] = {{
            "virtual_path": metadata.path,
            "size_bytes": metadata.size or 0,
            "content_hash": metadata.etag,
            "file_type": metadata.mime_type,
            "created_at": _to_naive(metadata.created_at) or _utcnow_naive(),
            "updated_at": _to_naive(metadata.modified_at) or _utcnow_naive(),
            "zone_id": metadata.zone_id or ROOT_ZONE_ID,
            "posix_uid": metadata.owner_id,
        }}
        if include_version:
            values["current_version"] = 1
        return values

    @staticmethod
    def to_file_path_update_values(metadata: FileMetadata) -> dict[str, Any]:
        """Convert FileMetadata to dict for UPDATE operations."""
        return {{
            "size_bytes": metadata.size or 0,
            "content_hash": metadata.etag,
            "file_type": metadata.mime_type,
            "updated_at": _to_naive(metadata.modified_at) or _utcnow_naive(),
        }}
'''


def generate_protobuf_stubs() -> None:
    """Generate metadata_pb2.py via grpc_tools.protoc.

    This produces the standard protobuf Python stubs used by
    RaftFileMetadataProtocol for binary serialization into redb.
    """
    try:
        from grpc_tools import protoc
    except ImportError:
        print(
            "WARNING: grpcio-tools not installed, skipping metadata_pb2.py generation.\n"
            "  Install with: uv add --dev grpcio-tools",
            file=sys.stderr,
        )
        return

    proto_include = str(REPO_ROOT / "proto")
    src_out = str(REPO_ROOT / "src")
    proto_file = "nexus/core/metadata.proto"

    result = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{proto_include}",
            f"--python_out={src_out}",
            f"--pyi_out={src_out}",
            proto_file,
        ]
    )

    if result != 0:
        print(f"ERROR: protoc failed with exit code {result}", file=sys.stderr)
        sys.exit(1)

    pb2_path = REPO_ROOT / "src" / "nexus" / "core" / "metadata_pb2.py"
    pyi_path = REPO_ROOT / "src" / "nexus" / "core" / "metadata_pb2.pyi"
    print(f"Generated: {pb2_path}")
    print(f"Generated: {pyi_path}")


def apply_renames(renames: dict[str, str]) -> list[str]:
    """Apply one-time renames to all downstream .py files.

    Scans src/ and tests/ for word-boundary matches of old names
    and replaces with new names. Skips generated files themselves.

    Returns list of modified file paths.
    """
    if not renames:
        return []

    src_dir = REPO_ROOT / "src"
    tests_dir = REPO_ROOT / "tests"

    # Build a single regex matching any old name (word-boundary safe)
    old_names = sorted(renames.keys(), key=len, reverse=True)  # longest first
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in old_names) + r")\b")

    # Files to skip (generated or managed by this script)
    skip = {
        METADATA_OUT.resolve(),
        METASTORE_OUT.resolve(),
        COMPACT_OUT.resolve(),
        MAPPER_OUT.resolve(),
    }

    modified = []
    for search_dir in [src_dir, tests_dir]:
        if not search_dir.exists():
            continue
        for py_file in search_dir.rglob("*.py"):
            if py_file.resolve() in skip:
                continue
            with open(py_file, encoding="utf-8", newline="") as fh:
                content = fh.read()
            new_content = pattern.sub(lambda m: renames[m.group(1)], content)
            if new_content != content:
                with open(py_file, "w", encoding="utf-8", newline="") as fh:
                    fh.write(new_content)
                modified.append(str(py_file.relative_to(REPO_ROOT)))

    return modified


def audit_ssot_coverage() -> list[str]:
    """Audit that all downstream imports from generated modules use valid names.

    Scans src/ and tests/ for imports from metadata, metastore,
    _compact_generated, and checks every imported name is in GENERATED_NAMES.

    Returns list of warnings (empty = all clean).
    """
    src_dir = REPO_ROOT / "src"
    tests_dir = REPO_ROOT / "tests"

    # Match single-line and multi-line imports from generated/managed modules
    # e.g. from nexus.contracts.metadata import FileMetadata
    # e.g. from nexus.core.metastore import MetastoreABC
    # e.g. from nexus.core._compact_generated import CompactFileMetadata
    import_re = re.compile(
        r"from\s+nexus\.core\.(metadata|metastore|_compact_generated)\s+import\s+"
        r"(?:\(([^)]*)\)|(.+?))\s*$",
        re.MULTILINE | re.DOTALL,
    )

    # Extract bare name from "Name as Alias" → "Name"
    def _parse_imported_names(names_str: str) -> set[str]:
        names = set()
        for token in names_str.split(","):
            token = token.strip().strip("()")
            if not token or token.startswith("#"):
                continue
            # Handle "Name as Alias" — we only care about the source name
            bare = token.split()[0] if token.split() else ""
            if bare and bare.isidentifier():
                names.add(bare)
        return names

    warnings = []
    for search_dir in [src_dir, tests_dir]:
        if not search_dir.exists():
            continue
        for py_file in search_dir.rglob("*.py"):
            with open(py_file, encoding="utf-8", newline="") as fh:
                content = fh.read()
            for m in import_re.finditer(content):
                module = m.group(1)
                names_str = m.group(2) or m.group(3) or ""
                imported = _parse_imported_names(names_str)
                valid = GENERATED_NAMES.get(module, set())
                for name in imported:
                    # Skip private names (tests may legitimately import internals)
                    if name.startswith("_"):
                        continue
                    if name not in valid:
                        rel = py_file.relative_to(REPO_ROOT)
                        warnings.append(f"  {rel}: imports '{name}' from {module} (not in SSOT)")

    return warnings


def main() -> None:
    """Parse proto and generate Python files."""
    if not PROTO_PATH.exists():
        print(f"ERROR: Proto file not found: {PROTO_PATH}", file=sys.stderr)
        sys.exit(1)

    enums = parse_proto_enums(PROTO_PATH)
    fields = parse_proto_fields(PROTO_PATH)
    if not fields:
        print("ERROR: No fields found in FileMetadata message", file=sys.stderr)
        sys.exit(1)

    if enums:
        print(f"Parsed {len(enums)} enum(s) from {PROTO_PATH.name}:")
        for ename, evals in enums.items():
            print(f"  {ename}: {', '.join(f'{v[0]}={v[1]}' for v in evals)}")

    print(f"Parsed {len(fields)} fields from {PROTO_PATH.name}:")
    for f in fields:
        print(f"  {f['type']} {f['name']} = {f['number']}")

    # 1. Generate protobuf stubs (metadata_pb2.py)
    generate_protobuf_stubs()

    # 2. Generate Python dataclass (FileMetadata)
    metadata_content = generate_metadata_py(fields, enums)
    METADATA_OUT.write_text(metadata_content, encoding="utf-8")
    print(f"Generated: {METADATA_OUT}")

    # 2b. MetastoreABC is hand-maintained in metastore.py
    # (not generated — the ABC methods are designed by hand, not derived from proto)
    print(f"Skipped:   {METASTORE_OUT} (hand-maintained)")

    # 3. Generate compact metadata (CompactFileMetadata + interning)
    compact_content = generate_compact_py(fields)
    COMPACT_OUT.write_text(compact_content, encoding="utf-8")
    print(f"Generated: {COMPACT_OUT}")

    # 4. Generate metadata mapper (MetadataMapper — proto/JSON serialization)
    mapper_content = generate_mapper_py(fields)
    MAPPER_OUT.write_text(mapper_content, encoding="utf-8")
    print(f"Generated: {MAPPER_OUT}")

    # 5. Apply one-time renames to downstream imports
    if RENAMES:
        print(f"\nApplying {len(RENAMES)} rename(s) to downstream files:")
        for old, new in RENAMES.items():
            print(f"  {old} → {new}")
        modified = apply_renames(RENAMES)
        if modified:
            print(f"Updated {len(modified)} files:")
            for f in sorted(modified):
                print(f"  {f}")
        else:
            print("No downstream files needed updating.")

    # 6. SSOT coverage audit
    print("\nSSOT coverage audit:")
    warnings = audit_ssot_coverage()
    if warnings:
        print(f"Found {len(warnings)} issue(s):")
        for w in warnings:
            print(w)
    else:
        print("  All downstream imports reference valid generated names.")

    print("\nDone. SSOT: proto/nexus/core/metadata.proto")


if __name__ == "__main__":
    main()
