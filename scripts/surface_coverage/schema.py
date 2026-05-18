"""Surface coverage data schema + YAML I/O.

One row per logical operation. Each transport cell is filled or None.
Human-filled fields (usage_example, correctness_test, perf_class, perf_link,
gap_issue, owning_issue) are None in v1 and populated by subissues.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


class ProfileStatus(StrEnum):
    SUPPORTED = "supported"
    UNAVAILABLE = "unavailable"
    ADMIN_ONLY = "admin_only"
    DEPRECATED = "deprecated"
    MISSING_NEEDED = "missing_needed"


class PerfClass(StrEnum):
    HOT = "hot"
    SETUP = "setup"
    CONTROL = "control"
    NOT_PERF_SENSITIVE = "not_perf_sensitive"


TRANSPORT_KEYS = ("cli", "grpc_typed", "grpc_call", "grpc_expose", "http", "mcp", "sdk")
PROFILE_KEYS = ("lite", "sandbox", "full")


@dataclass(frozen=True)
class TransportCell:
    name: str
    source: str  # "path/to/file.py:line"


@dataclass
class Module:
    id: str
    name: str
    description: str
    layer: str = ""  # one of the 5 architectural layers
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Operation:
    id: str  # canonical "<module>.<verb>"
    module: str
    summary: str
    transports: dict[str, TransportCell]  # subset of TRANSPORT_KEYS
    profiles: dict[str, ProfileStatus]  # exactly PROFILE_KEYS
    usage_example: str | None = None
    correctness_test: str | None = None
    perf_class: PerfClass | None = None
    perf_link: str | None = None
    gap_issue: int | None = None
    owning_issue: int | None = None


@dataclass
class ParityWarning:
    operation_id: str
    has: list[str]  # transport keys present
    missing: list[str]  # transport keys absent but expected


@dataclass
class UnmappedSurface:
    transport: str
    name: str
    source: str
    suggested_op_id: str | None = None


@dataclass
class StaleRow:
    operation_id: str
    reason: str


@dataclass
class SurfaceCoverage:
    schema_version: int
    modules: list[Module]
    operations: list[Operation]
    parity_warnings: list[ParityWarning] = field(default_factory=list)
    unmapped_surfaces: list[UnmappedSurface] = field(default_factory=list)
    stale_rows: list[StaleRow] = field(default_factory=list)


def _operation_to_dict(op: Operation) -> dict[str, Any]:
    return {
        "id": op.id,
        "module": op.module,
        "summary": op.summary,
        "transports": {k: asdict(v) for k, v in sorted(op.transports.items())},
        "profiles": {k: op.profiles[k].value for k in PROFILE_KEYS},
        "usage_example": op.usage_example,
        "correctness_test": op.correctness_test,
        "perf_class": op.perf_class.value if op.perf_class else None,
        "perf_link": op.perf_link,
        "gap_issue": op.gap_issue,
        "owning_issue": op.owning_issue,
    }


def _operation_from_dict(d: dict[str, Any]) -> Operation:
    transports = {
        k: TransportCell(name=v["name"], source=v["source"])
        for k, v in (d.get("transports") or {}).items()
    }
    profiles_raw = d.get("profiles") or {}
    profiles: dict[str, ProfileStatus] = {}
    for k in PROFILE_KEYS:
        raw = profiles_raw.get(k)
        if raw is None:
            raise ValueError(f"operation {d.get('id')} missing profile '{k}'")
        try:
            profiles[k] = ProfileStatus(raw)
        except ValueError as e:
            raise ValueError(f"operation {d.get('id')}: invalid profile status '{raw}'") from e
    perf_class_raw = d.get("perf_class")
    perf_class = PerfClass(perf_class_raw) if perf_class_raw else None
    return Operation(
        id=d["id"],
        module=d["module"],
        summary=d.get("summary", ""),
        transports=transports,
        profiles=profiles,
        usage_example=d.get("usage_example"),
        correctness_test=d.get("correctness_test"),
        perf_class=perf_class,
        perf_link=d.get("perf_link"),
        gap_issue=d.get("gap_issue"),
        owning_issue=d.get("owning_issue"),
    )


def load_yaml(path: Path) -> SurfaceCoverage:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError(f"unsupported schema_version: {data.get('schema_version')}")
    return SurfaceCoverage(
        schema_version=1,
        modules=[Module(**m) for m in data.get("modules", [])],
        operations=[_operation_from_dict(o) for o in data.get("operations", [])],
        parity_warnings=[ParityWarning(**w) for w in data.get("parity_warnings", [])],
        unmapped_surfaces=[UnmappedSurface(**u) for u in data.get("unmapped_surfaces", [])],
        stale_rows=[StaleRow(**s) for s in data.get("stale_rows", [])],
    )


def dump_yaml(coverage: SurfaceCoverage, path: Path) -> None:
    import os

    payload = {
        "schema_version": coverage.schema_version,
        "modules": [asdict(m) for m in coverage.modules],
        "operations": [_operation_to_dict(o) for o in coverage.operations],
        "parity_warnings": [asdict(w) for w in coverage.parity_warnings],
        "unmapped_surfaces": [asdict(u) for u in coverage.unmapped_surfaces],
        "stale_rows": [asdict(s) for s in coverage.stale_rows],
    }
    content = yaml.safe_dump(payload, sort_keys=False, width=120)
    # Atomic write: temp file in same dir, fsync, os.replace.
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)
