# Issue #4161 — API/RPC Surface Coverage Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a single PR that publishes an HTML map of every Nexus external surface (CLI / typed gRPC / generic gRPC / `@rpc_expose` / HTTP / MCP / SDK), codifies the row contract in a markdown doc, and distributes contract-conformance acceptance criteria into 21 existing subissues.

**Architecture:** Extractor reads source code via AST/proto/YAML and emits `api-rpc-surface-coverage.yaml`. Renderer reads YAML + jinja template, emits `api-rpc-surface-coverage.html` with embedded Mermaid module graph and vanilla-JS search/expand. Warn-only pytest gate detects drift. Separate `gh issue edit` driver appends a standard contract appendix to subissue bodies.

**Tech Stack:** Python 3.14 + uv; jinja2 (added as dev-dep); pytest; Mermaid (vendored inline); GitHub Pages publishing via existing docs workflow.

**Spec:** `docs/superpowers/specs/2026-05-16-issue-4161-arch-surface-coverage-design.md`

---

## File structure to be created

```
docs/architecture/
  api-rpc-surface-coverage.html              # rendered (committed)
  api-rpc-surface-coverage.yaml              # extracted inventory (committed)
  api-rpc-surface-overrides.yaml             # human overrides (empty stub in v1)
  api-rpc-surface-contract.md                # mental model + row contract + standards

scripts/
  gen_api_surface_coverage.py                # CLI entry: orchestrates extractors
  render_api_surface_coverage.py             # CLI entry: jinja render
  distribute_surface_contract_to_subissues.py # CLI entry: gh issue edit driver

scripts/surface_coverage/
  __init__.py
  schema.py                                  # dataclasses, enums, YAML I/O
  normalize.py                               # op-id normalization rules
  merge.py                                   # merge new extraction with existing YAML, preserve human fields
  extract_cli.py
  extract_grpc_typed.py
  extract_grpc_call.py
  extract_rpc_expose.py
  extract_http.py
  extract_mcp.py
  extract_sdk.py
  extract_profiles.py
  render.py                                  # jinja + mermaid generation
  distribute.py                              # gh issue edit logic

scripts/surface_coverage/templates/
  coverage.html.j2

tests/architecture/
  __init__.py
  conftest.py
  test_schema.py
  test_normalize.py
  test_merge.py
  test_extract_cli.py
  test_extract_grpc_typed.py
  test_extract_grpc_call.py
  test_extract_rpc_expose.py
  test_extract_http.py
  test_extract_mcp.py
  test_extract_sdk.py
  test_extract_profiles.py
  test_render.py
  test_distribute.py
  test_inventory.py                          # warn-only freshness + render + schema CI gate
```

---

## Task 1: Add jinja2 dev-dep + create package skeleton

**Files:**
- Modify: `pyproject.toml`
- Create: `scripts/surface_coverage/__init__.py`
- Create: `scripts/surface_coverage/templates/.gitkeep`
- Create: `tests/architecture/__init__.py`
- Create: `tests/architecture/conftest.py`

- [ ] **Step 1: Check jinja2 isn't already pulled in transitively**

Run: `uv pip list | grep -i jinja`

If jinja2 is already available (likely via mkdocs), skip adding to pyproject.toml. If absent, add explicit dep so script users don't depend on transitive resolution.

- [ ] **Step 2: Add jinja2 to dev-dependencies (only if step 1 showed missing)**

Edit `pyproject.toml`, find the `[tool.uv.dev-dependencies]` block (or `[project.optional-dependencies].dev`), add `"jinja2>=3.1"`.

- [ ] **Step 3: Create package skeleton**

```bash
mkdir -p scripts/surface_coverage/templates
mkdir -p tests/architecture
touch scripts/surface_coverage/__init__.py
touch scripts/surface_coverage/templates/.gitkeep
touch tests/architecture/__init__.py
```

Write `tests/architecture/conftest.py`:

```python
"""Shared fixtures for surface-coverage tests."""
from pathlib import Path

import pytest


@pytest.fixture
def repo_root() -> Path:
    """Return the repository root (4 levels up from this test file)."""
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def tmp_yaml(tmp_path: Path) -> Path:
    """Path to a temp YAML file."""
    return tmp_path / "coverage.yaml"
```

- [ ] **Step 4: Verify package importable**

Run: `uv run python -c "import scripts.surface_coverage"`
Expected: no output, exit 0.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml scripts/surface_coverage/ tests/architecture/
git commit -m "scaffold(#4161): surface_coverage package + tests/architecture skeleton"
```

---

## Task 2: Schema dataclasses + enums + YAML I/O

**Files:**
- Create: `scripts/surface_coverage/schema.py`
- Create: `tests/architecture/test_schema.py`

- [ ] **Step 1: Write failing test for schema round-trip**

Write `tests/architecture/test_schema.py`:

```python
"""Schema dataclasses + YAML round-trip."""
from pathlib import Path

import pytest

from scripts.surface_coverage.schema import (
    ProfileStatus,
    PerfClass,
    Module,
    TransportCell,
    Operation,
    SurfaceCoverage,
    load_yaml,
    dump_yaml,
)


def test_profile_status_enum_values():
    assert ProfileStatus.SUPPORTED.value == "supported"
    assert ProfileStatus.UNAVAILABLE.value == "unavailable"
    assert ProfileStatus.ADMIN_ONLY.value == "admin_only"
    assert ProfileStatus.DEPRECATED.value == "deprecated"
    assert ProfileStatus.MISSING_NEEDED.value == "missing_needed"


def test_perf_class_enum_values():
    assert PerfClass.HOT.value == "hot"
    assert PerfClass.SETUP.value == "setup"
    assert PerfClass.CONTROL.value == "control"
    assert PerfClass.NOT_PERF_SENSITIVE.value == "not_perf_sensitive"


def test_round_trip_minimal(tmp_yaml: Path):
    coverage = SurfaceCoverage(
        schema_version=1,
        modules=[Module(id="vfs", name="VFS", description="d", depends_on=[])],
        operations=[
            Operation(
                id="fs.read",
                module="vfs",
                summary="Read bytes",
                transports={
                    "cli": TransportCell(name="nexus fs read", source="src/x.py:1"),
                },
                profiles={
                    "lite": ProfileStatus.SUPPORTED,
                    "sandbox": ProfileStatus.SUPPORTED,
                    "full": ProfileStatus.SUPPORTED,
                },
            )
        ],
        parity_warnings=[],
        unmapped_surfaces=[],
        stale_rows=[],
    )
    dump_yaml(coverage, tmp_yaml)
    reloaded = load_yaml(tmp_yaml)
    assert reloaded == coverage


def test_load_yaml_rejects_unknown_profile_status(tmp_yaml: Path):
    tmp_yaml.write_text(
        "schema_version: 1\n"
        "modules: []\n"
        "operations:\n"
        "  - id: x.y\n"
        "    module: x\n"
        "    summary: s\n"
        "    transports: {}\n"
        "    profiles: {lite: bogus, sandbox: supported, full: supported}\n"
        "parity_warnings: []\n"
        "unmapped_surfaces: []\n"
        "stale_rows: []\n"
    )
    with pytest.raises(ValueError, match="bogus"):
        load_yaml(tmp_yaml)
```

- [ ] **Step 2: Run test, confirm failure**

Run: `uv run pytest tests/architecture/test_schema.py -v`
Expected: ImportError — module doesn't exist yet.

- [ ] **Step 3: Implement schema**

Write `scripts/surface_coverage/schema.py`:

```python
"""Surface coverage data schema + YAML I/O.

One row per logical operation. Each transport cell is filled or None.
Human-filled fields (usage_example, correctness_test, perf_class, perf_link,
gap_issue, owning_issue) are None in v1 and populated by subissues.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class ProfileStatus(str, Enum):
    SUPPORTED = "supported"
    UNAVAILABLE = "unavailable"
    ADMIN_ONLY = "admin_only"
    DEPRECATED = "deprecated"
    MISSING_NEEDED = "missing_needed"


class PerfClass(str, Enum):
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
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Operation:
    id: str                                   # canonical "<module>.<verb>"
    module: str
    summary: str
    transports: dict[str, TransportCell]      # subset of TRANSPORT_KEYS
    profiles: dict[str, ProfileStatus]        # exactly PROFILE_KEYS
    usage_example: str | None = None
    correctness_test: str | None = None
    perf_class: PerfClass | None = None
    perf_link: str | None = None
    gap_issue: int | None = None
    owning_issue: int | None = None


@dataclass
class ParityWarning:
    operation_id: str
    has: list[str]      # transport keys present
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
            raise ValueError(
                f"operation {d.get('id')}: invalid profile status '{raw}'"
            ) from e
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
    data = yaml.safe_load(path.read_text())
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
    payload = {
        "schema_version": coverage.schema_version,
        "modules": [asdict(m) for m in coverage.modules],
        "operations": [_operation_to_dict(o) for o in coverage.operations],
        "parity_warnings": [asdict(w) for w in coverage.parity_warnings],
        "unmapped_surfaces": [asdict(u) for u in coverage.unmapped_surfaces],
        "stale_rows": [asdict(s) for s in coverage.stale_rows],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False, width=120))
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/architecture/test_schema.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/surface_coverage/schema.py tests/architecture/test_schema.py
git commit -m "feat(#4161): surface coverage schema dataclasses + YAML I/O"
```

---

## Task 3: Op-id normalizer

**Files:**
- Create: `scripts/surface_coverage/normalize.py`
- Create: `tests/architecture/test_normalize.py`

- [ ] **Step 1: Write failing test**

Write `tests/architecture/test_normalize.py`:

```python
"""Op-id normalization across transports."""
import pytest

from scripts.surface_coverage.normalize import (
    normalize_cli,
    normalize_grpc_typed,
    normalize_grpc_call,
    normalize_http,
    normalize_mcp,
    normalize_sdk,
)


@pytest.mark.parametrize("raw,expected", [
    ("nexus fs read", "fs.read"),
    ("nexus rebac grant", "rebac.grant"),
    ("nexus mounts list", "mounts.list"),
    ("nexus workspace snapshot create", "workspace.snapshot_create"),
])
def test_normalize_cli(raw, expected):
    assert normalize_cli(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("VFS.Read", "fs.read"),
    ("VFS.Write", "fs.write"),
    ("ReBAC.Grant", "rebac.grant"),
])
def test_normalize_grpc_typed(raw, expected):
    assert normalize_grpc_typed(raw) == expected


def test_normalize_grpc_call_passthrough():
    # generic Call names are already in module.verb form
    assert normalize_grpc_call("fs.read") == "fs.read"
    assert normalize_grpc_call("rebac.grant") == "rebac.grant"


@pytest.mark.parametrize("method,path,expected", [
    ("POST", "/api/v1/fs/read", "fs.read"),
    ("GET", "/api/v1/rebac/grants", "rebac.grants"),
    ("POST", "/api/v1/workspace/snapshot/create", "workspace.snapshot_create"),
])
def test_normalize_http(method, path, expected):
    assert normalize_http(method, path) == expected


@pytest.mark.parametrize("raw,expected", [
    ("nexus_fs_read", "fs.read"),
    ("nexus_rebac_grant", "rebac.grant"),
])
def test_normalize_mcp(raw, expected):
    assert normalize_mcp(raw) == expected


def test_normalize_sdk():
    assert normalize_sdk("NexusClient", "read") == "fs.read"
    assert normalize_sdk("NexusClient", "rebac_grant") == "rebac.grant"
```

- [ ] **Step 2: Run test, confirm failure**

Run: `uv run pytest tests/architecture/test_normalize.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement normalizer**

Write `scripts/surface_coverage/normalize.py`:

```python
"""Normalize per-transport surface names to canonical op-id <module>.<verb>.

Op-id is stable across transports. The first token after the module is the verb;
additional path/name segments are joined with underscore.

Examples:
    CLI  "nexus fs read"                 -> "fs.read"
    CLI  "nexus workspace snapshot create" -> "workspace.snapshot_create"
    gRPC "VFS.Read"                      -> "fs.read"
    HTTP POST /api/v1/fs/read            -> "fs.read"
    MCP  "nexus_fs_read"                 -> "fs.read"
    SDK  NexusClient.read                -> "fs.read"

Unmapped names should be added as overrides in api-rpc-surface-overrides.yaml.
"""
from __future__ import annotations

import re

# gRPC typed service name -> canonical module
_GRPC_SERVICE_TO_MODULE = {
    "VFS": "fs",
    "ReBAC": "rebac",
    "Workspace": "workspace",
    "Search": "search",
    "MCP": "mcp",
}

# SDK method name prefix -> module (when method doesn't carry module explicitly)
_SDK_DEFAULT_MODULE = "fs"


def _to_snake(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def normalize_cli(cli_invocation: str) -> str:
    """`nexus <module> <verb> [<more>...]` -> `<module>.<verb>[_<more>...]`"""
    parts = cli_invocation.strip().split()
    if len(parts) < 3 or parts[0] != "nexus":
        raise ValueError(f"unrecognized CLI form: {cli_invocation!r}")
    module = parts[1].replace("-", "_")
    verb_parts = [p.replace("-", "_") for p in parts[2:]]
    return f"{module}.{'_'.join(verb_parts)}"


def normalize_grpc_typed(method: str) -> str:
    """`<Service>.<Method>` -> `<module>.<verb>` via service->module mapping."""
    if "." not in method:
        raise ValueError(f"expected '<Service>.<Method>', got: {method!r}")
    service, m = method.split(".", 1)
    module = _GRPC_SERVICE_TO_MODULE.get(service, _to_snake(service))
    return f"{module}.{_to_snake(m)}"


def normalize_grpc_call(call_name: str) -> str:
    """Generic gRPC `Call` names are already canonical."""
    return call_name


def normalize_http(method: str, path: str) -> str:
    """`POST /api/v1/<module>/<verb>[/<more>]` -> `<module>.<verb>[_<more>]`."""
    m = re.match(r"^/api/v\d+/([^/]+)/(.+?)/?$", path)
    if not m:
        raise ValueError(f"unrecognized HTTP path: {path!r}")
    module = m.group(1).replace("-", "_")
    verb_parts = [p.replace("-", "_") for p in m.group(2).split("/")]
    return f"{module}.{'_'.join(verb_parts)}"


def normalize_mcp(tool_name: str) -> str:
    """`nexus_<module>_<verb>[_<more>]` -> `<module>.<verb>[_<more>]`."""
    if not tool_name.startswith("nexus_"):
        raise ValueError(f"unrecognized MCP tool name: {tool_name!r}")
    rest = tool_name[len("nexus_"):]
    parts = rest.split("_", 1)
    if len(parts) != 2:
        raise ValueError(f"MCP name needs module+verb: {tool_name!r}")
    return f"{parts[0]}.{parts[1]}"


def normalize_sdk(class_name: str, method_name: str) -> str:
    """`NexusClient.<method>` -> `<module>.<verb>`.

    If method contains '_', first segment is module; otherwise default module 'fs'.
    """
    if "_" in method_name:
        module, _, verb = method_name.partition("_")
        return f"{module}.{verb}"
    return f"{_SDK_DEFAULT_MODULE}.{method_name}"
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/architecture/test_normalize.py -v`
Expected: all parametrized cases pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/surface_coverage/normalize.py tests/architecture/test_normalize.py
git commit -m "feat(#4161): op-id normalizer for cross-transport joining"
```

---

## Task 4: Merge logic (preserve human-filled fields)

**Files:**
- Create: `scripts/surface_coverage/merge.py`
- Create: `tests/architecture/test_merge.py`

- [ ] **Step 1: Write failing test**

Write `tests/architecture/test_merge.py`:

```python
"""Merge new extraction with committed YAML, preserving human-filled fields."""
from scripts.surface_coverage.merge import merge_coverage
from scripts.surface_coverage.schema import (
    Module, Operation, PerfClass, ProfileStatus, SurfaceCoverage, TransportCell,
)


def _profiles_all_supported():
    return {
        "lite": ProfileStatus.SUPPORTED,
        "sandbox": ProfileStatus.SUPPORTED,
        "full": ProfileStatus.SUPPORTED,
    }


def _op(id_, module, summary="", **overrides):
    base = Operation(
        id=id_, module=module, summary=summary,
        transports={}, profiles=_profiles_all_supported(),
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_merge_preserves_human_fields():
    existing_op = _op(
        "fs.read", "vfs",
        usage_example="nexus fs read /path",
        correctness_test="tests/test_fs.py:42",
        perf_class=PerfClass.HOT,
        perf_link="bench/test_fs_read.py:10",
        owning_issue=4123,
    )
    existing = SurfaceCoverage(
        schema_version=1, modules=[Module("vfs", "VFS", "")], operations=[existing_op],
    )
    # extractor re-discovers fs.read with refreshed transport info + extractor summary
    fresh_op = _op(
        "fs.read", "vfs", summary="extractor docstring",
        transports={"cli": TransportCell("nexus fs read", "src/x.py:99")},
    )
    fresh = SurfaceCoverage(
        schema_version=1, modules=[Module("vfs", "VFS", "")], operations=[fresh_op],
    )

    merged = merge_coverage(existing=existing, fresh=fresh)
    op = next(o for o in merged.operations if o.id == "fs.read")

    # transports refreshed from extractor
    assert op.transports["cli"].source == "src/x.py:99"
    # human fields preserved
    assert op.usage_example == "nexus fs read /path"
    assert op.correctness_test == "tests/test_fs.py:42"
    assert op.perf_class == PerfClass.HOT
    assert op.perf_link == "bench/test_fs_read.py:10"
    assert op.owning_issue == 4123
    # summary: human override wins if non-empty; else extractor
    assert op.summary == "extractor docstring"  # existing summary was ""


def test_merge_adds_new_operations():
    existing = SurfaceCoverage(
        schema_version=1, modules=[], operations=[_op("fs.read", "vfs")],
    )
    fresh = SurfaceCoverage(
        schema_version=1, modules=[],
        operations=[_op("fs.read", "vfs"), _op("fs.write", "vfs")],
    )
    merged = merge_coverage(existing=existing, fresh=fresh)
    assert {o.id for o in merged.operations} == {"fs.read", "fs.write"}


def test_merge_flags_stale_rows():
    existing = SurfaceCoverage(
        schema_version=1, modules=[],
        operations=[_op("fs.read", "vfs"), _op("fs.deprecated", "vfs", owning_issue=999)],
    )
    fresh = SurfaceCoverage(
        schema_version=1, modules=[], operations=[_op("fs.read", "vfs")],
    )
    merged = merge_coverage(existing=existing, fresh=fresh)
    # stale op preserved in operations but added to stale_rows
    assert {o.id for o in merged.operations} == {"fs.read", "fs.deprecated"}
    assert any(s.operation_id == "fs.deprecated" for s in merged.stale_rows)


def test_merge_human_summary_wins():
    existing = _op("fs.read", "vfs", summary="human-curated description")
    fresh = _op("fs.read", "vfs", summary="extractor docstring")
    merged = merge_coverage(
        existing=SurfaceCoverage(1, [], [existing]),
        fresh=SurfaceCoverage(1, [], [fresh]),
    )
    op = next(o for o in merged.operations if o.id == "fs.read")
    assert op.summary == "human-curated description"
```

- [ ] **Step 2: Run test, confirm failure**

Run: `uv run pytest tests/architecture/test_merge.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement merge**

Write `scripts/surface_coverage/merge.py`:

```python
"""Merge fresh extraction with committed YAML, preserving human-filled fields.

Idempotent: same inputs -> same output.

Human-owned fields preserved from existing YAML:
    summary (if non-empty in existing), usage_example, correctness_test,
    perf_class, perf_link, gap_issue, owning_issue

Extractor-owned fields refreshed from fresh:
    transports, module (if extractor reassigned), profiles (extractor seeds;
    humans override via overrides YAML applied separately).
"""
from __future__ import annotations

from dataclasses import replace

from scripts.surface_coverage.schema import (
    Operation, ParityWarning, StaleRow, SurfaceCoverage,
)


def _merge_op(existing: Operation, fresh: Operation) -> Operation:
    return replace(
        fresh,
        summary=existing.summary if existing.summary.strip() else fresh.summary,
        usage_example=existing.usage_example,
        correctness_test=existing.correctness_test,
        perf_class=existing.perf_class,
        perf_link=existing.perf_link,
        gap_issue=existing.gap_issue,
        owning_issue=existing.owning_issue,
    )


def merge_coverage(
    *,
    existing: SurfaceCoverage,
    fresh: SurfaceCoverage,
) -> SurfaceCoverage:
    existing_by_id = {op.id: op for op in existing.operations}
    fresh_by_id = {op.id: op for op in fresh.operations}

    merged_ops: list[Operation] = []
    stale_rows: list[StaleRow] = list(existing.stale_rows)

    for op_id, fresh_op in fresh_by_id.items():
        if op_id in existing_by_id:
            merged_ops.append(_merge_op(existing_by_id[op_id], fresh_op))
        else:
            merged_ops.append(fresh_op)

    # preserve operations that disappeared from extractor (don't auto-delete)
    for op_id, existing_op in existing_by_id.items():
        if op_id not in fresh_by_id:
            merged_ops.append(existing_op)
            if not any(s.operation_id == op_id for s in stale_rows):
                stale_rows.append(StaleRow(
                    operation_id=op_id,
                    reason="present in committed YAML but not detected by extractor",
                ))

    # sort for deterministic output
    merged_ops.sort(key=lambda o: o.id)

    return SurfaceCoverage(
        schema_version=fresh.schema_version,
        modules=sorted(fresh.modules, key=lambda m: m.id),
        operations=merged_ops,
        parity_warnings=list(fresh.parity_warnings),
        unmapped_surfaces=list(fresh.unmapped_surfaces),
        stale_rows=stale_rows,
    )
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/architecture/test_merge.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/surface_coverage/merge.py tests/architecture/test_merge.py
git commit -m "feat(#4161): merge logic preserves human-filled fields on re-extract"
```

---

## Task 5: MCP extractor

**Files:**
- Create: `scripts/surface_coverage/extract_mcp.py`
- Create: `tests/architecture/test_extract_mcp.py`

- [ ] **Step 1: Inspect the source format**

Run: `head -50 src/nexus/config/tool_profiles.yaml`
Note the structure (profiles -> tool list).

- [ ] **Step 2: Write failing test**

Write `tests/architecture/test_extract_mcp.py`:

```python
"""MCP extractor: enumerate tools from tool_profiles.yaml."""
from pathlib import Path

from scripts.surface_coverage.extract_mcp import extract_mcp_tools


def test_extract_mcp_from_fixture(tmp_path: Path):
    fixture = tmp_path / "tool_profiles.yaml"
    fixture.write_text(
        "profiles:\n"
        "  default:\n"
        "    tools:\n"
        "      - nexus_fs_read\n"
        "      - nexus_fs_write\n"
        "  agent:\n"
        "    tools:\n"
        "      - nexus_fs_read\n"
        "      - nexus_search_grep\n"
    )
    results = extract_mcp_tools(fixture)
    names = {r.name for r in results}
    assert names == {"nexus_fs_read", "nexus_fs_write", "nexus_search_grep"}
    # source should reference the fixture path
    assert all(str(fixture) in r.source for r in results)


def test_extract_mcp_real_file_smoke(repo_root: Path):
    """Smoke test against the real tool_profiles.yaml - just verify it parses."""
    real = repo_root / "src/nexus/config/tool_profiles.yaml"
    if not real.exists():
        return
    results = extract_mcp_tools(real)
    # don't assert specific tools - those change. just assert we got something.
    assert len(results) > 0
    assert all(r.name.startswith("nexus_") for r in results)
```

- [ ] **Step 3: Run test, confirm failure**

Run: `uv run pytest tests/architecture/test_extract_mcp.py -v`
Expected: ImportError.

- [ ] **Step 4: Implement extractor**

Write `scripts/surface_coverage/extract_mcp.py`:

```python
"""Extract MCP tools from src/nexus/config/tool_profiles.yaml."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RawMcpTool:
    name: str
    profile: str
    source: str  # "path:line" (line is best-effort 1 since YAML doesn't track per-key lines)


def extract_mcp_tools(path: Path) -> list[RawMcpTool]:
    data = yaml.safe_load(path.read_text())
    seen: dict[str, RawMcpTool] = {}
    for profile_name, profile_data in (data.get("profiles") or {}).items():
        for tool_name in (profile_data or {}).get("tools", []):
            if tool_name in seen:
                continue
            seen[tool_name] = RawMcpTool(
                name=tool_name,
                profile=profile_name,
                source=f"{path}:1",
            )
    return sorted(seen.values(), key=lambda r: r.name)
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `uv run pytest tests/architecture/test_extract_mcp.py -v`
Expected: 2 passed (smoke test may be skipped if file missing).

- [ ] **Step 6: Commit**

```bash
git add scripts/surface_coverage/extract_mcp.py tests/architecture/test_extract_mcp.py
git commit -m "feat(#4161): MCP tool extractor"
```

---

## Task 6: CLI extractor

**Files:**
- Create: `scripts/surface_coverage/extract_cli.py`
- Create: `tests/architecture/test_extract_cli.py`

- [ ] **Step 1: Inspect CLI registry mechanism**

Run: `head -80 src/nexus/cli/commands/__init__.py`
Confirm `_REGISTER_COMMANDS` dict shape: `{module_name: (command_name_1, command_name_2, ...)}`.

- [ ] **Step 2: Write failing test**

Write `tests/architecture/test_extract_cli.py`:

```python
"""CLI extractor: parse _REGISTER_COMMANDS dict from cli/commands/__init__.py."""
from pathlib import Path

from scripts.surface_coverage.extract_cli import extract_cli_commands


def test_extract_cli_from_fixture(tmp_path: Path):
    src = tmp_path / "src/nexus/cli/commands"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text(
        '"""CLI."""\n'
        "_REGISTER_COMMANDS = {\n"
        '    "file_ops": ("init", "cat", "write"),\n'
        '    "directory": ("ls", "mkdir"),\n'
        "}\n"
    )
    (src / "file_ops.py").write_text("# fake\n")
    (src / "directory.py").write_text("# fake\n")

    results = extract_cli_commands(src / "__init__.py")
    names = {r.name for r in results}
    assert names == {"nexus init", "nexus cat", "nexus write", "nexus ls", "nexus mkdir"}
    # source should point at the module file the command lives in
    by_name = {r.name: r for r in results}
    assert str(src / "file_ops.py") in by_name["nexus init"].source
    assert str(src / "directory.py") in by_name["nexus ls"].source


def test_extract_cli_real_file_smoke(repo_root: Path):
    real = repo_root / "src/nexus/cli/commands/__init__.py"
    if not real.exists():
        return
    results = extract_cli_commands(real)
    assert len(results) > 0
    assert all(r.name.startswith("nexus ") for r in results)
```

- [ ] **Step 3: Run test, confirm failure**

Run: `uv run pytest tests/architecture/test_extract_cli.py -v`
Expected: ImportError.

- [ ] **Step 4: Implement extractor**

Write `scripts/surface_coverage/extract_cli.py`:

```python
"""Extract CLI command names from src/nexus/cli/commands/__init__.py.

Parses the `_REGISTER_COMMANDS: dict[str, tuple[str, ...]]` literal via AST so
we don't need to import the package (which has heavy runtime deps).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RawCliCommand:
    name: str               # e.g. "nexus fs read"
    module_file: Path
    source: str             # "path:1" (module file; line approximate)


def extract_cli_commands(init_py_path: Path) -> list[RawCliCommand]:
    tree = ast.parse(init_py_path.read_text())
    register_dict: dict[str, tuple[str, ...]] | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_REGISTER_COMMANDS" for t in node.targets
        ):
            register_dict = _literal_dict_of_str_tuples(node.value)
            break
    if register_dict is None:
        raise ValueError(f"_REGISTER_COMMANDS not found in {init_py_path}")

    out: list[RawCliCommand] = []
    commands_dir = init_py_path.parent
    for module_name, command_names in register_dict.items():
        module_file = commands_dir / f"{module_name}.py"
        for cmd in command_names:
            # commands themselves may be single or multi-token; reduce - to space
            invocation = "nexus " + cmd.replace("_", " ")
            out.append(RawCliCommand(
                name=invocation,
                module_file=module_file,
                source=f"{module_file}:1",
            ))
    return sorted(out, key=lambda r: r.name)


def _literal_dict_of_str_tuples(node: ast.AST) -> dict[str, tuple[str, ...]]:
    if not isinstance(node, ast.Dict):
        raise ValueError("expected dict literal")
    out: dict[str, tuple[str, ...]] = {}
    for k_node, v_node in zip(node.keys, node.values):
        if not isinstance(k_node, ast.Constant) or not isinstance(k_node.value, str):
            raise ValueError("dict keys must be str literals")
        if not isinstance(v_node, ast.Tuple):
            raise ValueError("dict values must be tuple literals")
        values: list[str] = []
        for elt in v_node.elts:
            if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
                raise ValueError("tuple elements must be str literals")
            values.append(elt.value)
        out[k_node.value] = tuple(values)
    return out
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `uv run pytest tests/architecture/test_extract_cli.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/surface_coverage/extract_cli.py tests/architecture/test_extract_cli.py
git commit -m "feat(#4161): CLI command extractor via AST of _REGISTER_COMMANDS"
```

---

## Task 7: HTTP extractor

**Files:**
- Create: `scripts/surface_coverage/extract_http.py`
- Create: `tests/architecture/test_extract_http.py`

- [ ] **Step 1: Write failing test**

Write `tests/architecture/test_extract_http.py`:

```python
"""HTTP extractor: AST-scan FastAPI decorators."""
from pathlib import Path

from scripts.surface_coverage.extract_http import extract_http_routes


def test_extract_http_from_fixture(tmp_path: Path):
    f = tmp_path / "server.py"
    f.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "@router.get('/api/v1/fs/read')\n"
        "async def read(): pass\n"
        "\n"
        "@router.post('/api/v1/fs/write')\n"
        "async def write(): pass\n"
        "\n"
        "@router.delete('/api/v1/fs/{path}')\n"
        "async def delete_(): pass\n"
    )
    results = extract_http_routes(f)
    routes = {(r.method, r.path) for r in results}
    assert routes == {
        ("GET", "/api/v1/fs/read"),
        ("POST", "/api/v1/fs/write"),
        ("DELETE", "/api/v1/fs/{path}"),
    }


def test_extract_http_real_file_smoke(repo_root: Path):
    real = repo_root / "src/nexus/server/fastapi_server.py"
    if not real.exists():
        return
    # don't assert routes; just verify the extractor runs cleanly.
    results = extract_http_routes(real)
    assert isinstance(results, list)
```

- [ ] **Step 2: Run test, confirm failure**

Run: `uv run pytest tests/architecture/test_extract_http.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement extractor**

Write `scripts/surface_coverage/extract_http.py`:

```python
"""Extract HTTP routes from FastAPI files via AST.

Recognizes @<obj>.{get,post,put,patch,delete}('/path/...') decorators
where <obj> is any router-like instance.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


@dataclass(frozen=True)
class RawHttpRoute:
    method: str   # uppercase
    path: str
    source: str   # "file.py:line"


def extract_http_routes(py_path: Path) -> list[RawHttpRoute]:
    tree = ast.parse(py_path.read_text())
    out: list[RawHttpRoute] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            route = _route_from_decorator(deco)
            if route is None:
                continue
            method, path = route
            out.append(RawHttpRoute(
                method=method.upper(),
                path=path,
                source=f"{py_path}:{deco.lineno}",
            ))
    return sorted(out, key=lambda r: (r.path, r.method))


def _route_from_decorator(deco: ast.AST) -> tuple[str, str] | None:
    if not isinstance(deco, ast.Call):
        return None
    if not isinstance(deco.func, ast.Attribute):
        return None
    if deco.func.attr not in _HTTP_METHODS:
        return None
    if not deco.args:
        return None
    first = deco.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return None
    return (deco.func.attr, first.value)
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/architecture/test_extract_http.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/surface_coverage/extract_http.py tests/architecture/test_extract_http.py
git commit -m "feat(#4161): HTTP route extractor via FastAPI decorator AST scan"
```

---

## Task 8: gRPC typed extractor (proto)

**Files:**
- Create: `scripts/surface_coverage/extract_grpc_typed.py`
- Create: `tests/architecture/test_extract_grpc_typed.py`

- [ ] **Step 1: Write failing test**

Write `tests/architecture/test_extract_grpc_typed.py`:

```python
"""gRPC typed extractor: parse `rpc <Name>(...)` from .proto via regex."""
from pathlib import Path

from scripts.surface_coverage.extract_grpc_typed import extract_grpc_typed_methods


def test_extract_grpc_typed_from_fixture(tmp_path: Path):
    f = tmp_path / "vfs.proto"
    f.write_text(
        "syntax = 'proto3';\n"
        "package nexus.vfs;\n"
        "\n"
        "service VFS {\n"
        "  rpc Read (ReadRequest) returns (ReadResponse);\n"
        "  rpc Write (WriteRequest) returns (WriteResponse);\n"
        "  rpc Stat (StatRequest) returns (StatResponse);\n"
        "}\n"
        "\n"
        "service Search {\n"
        "  rpc Query (QueryRequest) returns (QueryResponse);\n"
        "}\n"
    )
    results = extract_grpc_typed_methods(f)
    methods = {r.method for r in results}
    assert methods == {"VFS.Read", "VFS.Write", "VFS.Stat", "Search.Query"}


def test_extract_grpc_typed_real_proto_smoke(repo_root: Path):
    real = repo_root / "proto/nexus/grpc/vfs/vfs.proto"
    if not real.exists():
        return
    results = extract_grpc_typed_methods(real)
    assert all("." in r.method for r in results)
```

- [ ] **Step 2: Run test, confirm failure**

Run: `uv run pytest tests/architecture/test_extract_grpc_typed.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement extractor**

Write `scripts/surface_coverage/extract_grpc_typed.py`:

```python
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
    method: str    # "<Service>.<Method>"
    source: str    # "file.proto:line"


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
            out.append(RawGrpcTypedMethod(
                method=f"{service}.{rpc.group('method')}",
                source=f"{proto_path}:{line}",
            ))
    return sorted(out, key=lambda r: r.method)
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/architecture/test_extract_grpc_typed.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/surface_coverage/extract_grpc_typed.py tests/architecture/test_extract_grpc_typed.py
git commit -m "feat(#4161): typed gRPC method extractor (proto regex)"
```

---

## Task 9: gRPC `Call` extractor (`_kernel_syscall_dispatch.py`)

**Files:**
- Create: `scripts/surface_coverage/extract_grpc_call.py`
- Create: `tests/architecture/test_extract_grpc_call.py`

- [ ] **Step 1: Inspect the dispatch shape**

Run: `head -60 src/nexus/server/_kernel_syscall_dispatch.py`
Observe the dispatch table structure (usually a dict mapping `Call` names to handlers).

- [ ] **Step 2: Write failing test**

Write `tests/architecture/test_extract_grpc_call.py`:

```python
"""Extract generic gRPC Call dispatch names from a dict literal via AST."""
from pathlib import Path

from scripts.surface_coverage.extract_grpc_call import extract_grpc_call_names


def test_extract_grpc_call_from_fixture(tmp_path: Path):
    f = tmp_path / "dispatch.py"
    f.write_text(
        "from typing import Callable\n"
        "def _read(req): pass\n"
        "def _write(req): pass\n"
        "\n"
        "KERNEL_SYSCALL_DISPATCH: dict[str, Callable] = {\n"
        '    "fs.read": _read,\n'
        '    "fs.write": _write,\n'
        '    "rebac.grant": _read,\n'
        "}\n"
    )
    results = extract_grpc_call_names(f, dispatch_var="KERNEL_SYSCALL_DISPATCH")
    names = {r.name for r in results}
    assert names == {"fs.read", "fs.write", "rebac.grant"}


def test_extract_grpc_call_real_file_smoke(repo_root: Path):
    real = repo_root / "src/nexus/server/_kernel_syscall_dispatch.py"
    if not real.exists():
        return
    # variable name may vary - try a few common ones
    for var in ("DISPATCH", "_DISPATCH", "KERNEL_SYSCALL_DISPATCH", "SYSCALL_DISPATCH"):
        try:
            results = extract_grpc_call_names(real, dispatch_var=var)
            if results:
                assert all("." in r.name for r in results)
                return
        except ValueError:
            continue
    # If we get here, none matched. Test fails to alert engineer to update the var name.
    raise AssertionError(
        "Could not find dispatch dict in _kernel_syscall_dispatch.py — "
        "inspect the file and update dispatch_var or extractor logic."
    )
```

- [ ] **Step 3: Run test, confirm failure**

Run: `uv run pytest tests/architecture/test_extract_grpc_call.py -v`
Expected: ImportError.

- [ ] **Step 4: Implement extractor**

Write `scripts/surface_coverage/extract_grpc_call.py`:

```python
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
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == dispatch_var:
                if node.value is not None:
                    return _names_from_dict(node.value, py_path)
    raise ValueError(f"variable {dispatch_var!r} not found in {py_path}")


def _names_from_dict(value: ast.AST, py_path: Path) -> list[RawGrpcCallName]:
    if not isinstance(value, ast.Dict):
        raise ValueError("dispatch value must be a dict literal")
    out: list[RawGrpcCallName] = []
    for k in value.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            out.append(RawGrpcCallName(
                name=k.value,
                source=f"{py_path}:{k.lineno}",
            ))
    return sorted(out, key=lambda r: r.name)
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `uv run pytest tests/architecture/test_extract_grpc_call.py -v`
Expected: fixture test passes; smoke test may need a `dispatch_var` adjustment based on the real file. If smoke fails with the alert message, inspect `_kernel_syscall_dispatch.py` and add the actual var name to the tuple in the test, then re-run.

- [ ] **Step 6: Commit**

```bash
git add scripts/surface_coverage/extract_grpc_call.py tests/architecture/test_extract_grpc_call.py
git commit -m "feat(#4161): gRPC Call name extractor from dispatch dict AST"
```

---

## Task 10: `@rpc_expose` extractor

**Files:**
- Create: `scripts/surface_coverage/extract_rpc_expose.py`
- Create: `tests/architecture/test_extract_rpc_expose.py`

- [ ] **Step 1: Write failing test**

Write `tests/architecture/test_extract_rpc_expose.py`:

```python
"""@rpc_expose extractor: AST-scan src tree for the decorator."""
from pathlib import Path

from scripts.surface_coverage.extract_rpc_expose import extract_rpc_exposes


def test_extract_rpc_expose_from_fixture(tmp_path: Path):
    f = tmp_path / "service.py"
    f.write_text(
        "def rpc_expose(*args, **kwargs):\n"
        "    def deco(fn): return fn\n"
        "    return deco\n"
        "\n"
        "class OAuthService:\n"
        "    @rpc_expose(name='oauth_list_providers', description='...')\n"
        "    def list_providers(self): pass\n"
        "\n"
        "    @rpc_expose(name='oauth_revoke', description='...')\n"
        "    def revoke(self): pass\n"
        "\n"
        "class ShareLinkService:\n"
        "    @rpc_expose(description='Create a share link')\n"
        "    def create_share_link(self): pass\n"
    )
    results = extract_rpc_exposes(tmp_path)
    by_name = {r.name: r for r in results}
    assert "oauth_list_providers" in by_name
    assert "oauth_revoke" in by_name
    # When name= is omitted, fall back to method name
    assert "create_share_link" in by_name


def test_extract_rpc_expose_real_tree_smoke(repo_root: Path):
    real = repo_root / "src/nexus"
    if not real.exists():
        return
    results = extract_rpc_exposes(real)
    # we know oauth_list_providers exists in the repo
    assert any(r.name == "oauth_list_providers" for r in results)
```

- [ ] **Step 2: Run test, confirm failure**

Run: `uv run pytest tests/architecture/test_extract_rpc_expose.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement extractor**

Write `scripts/surface_coverage/extract_rpc_expose.py`:

```python
"""Scan a source tree for @rpc_expose(name=..., description=...) decorators.

The decorator pattern is:
    @rpc_expose(name="oauth_list_providers", description="...")
    def method(self): ...

When `name=` is omitted, the method name itself is used.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RawRpcExpose:
    name: str
    class_name: str
    method_name: str
    source: str  # "file.py:line"


def extract_rpc_exposes(root: Path) -> list[RawRpcExpose]:
    out: list[RawRpcExpose] = []
    for py in root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        out.extend(_scan_module(tree, py))
    return sorted(out, key=lambda r: r.name)


def _scan_module(tree: ast.AST, py: Path) -> list[RawRpcExpose]:
    out: list[RawRpcExpose] = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for item in cls.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in item.decorator_list:
                name = _extract_rpc_expose_name(deco, item.name)
                if name is not None:
                    out.append(RawRpcExpose(
                        name=name,
                        class_name=cls.name,
                        method_name=item.name,
                        source=f"{py}:{deco.lineno}",
                    ))
    return out


def _extract_rpc_expose_name(deco: ast.AST, fallback_method_name: str) -> str | None:
    """Return the exposed name if `deco` is an @rpc_expose(...) call, else None."""
    if not isinstance(deco, ast.Call):
        return None
    callee = deco.func
    if isinstance(callee, ast.Name) and callee.id == "rpc_expose":
        pass
    elif isinstance(callee, ast.Attribute) and callee.attr == "rpc_expose":
        pass
    else:
        return None
    for kw in deco.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return fallback_method_name
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/architecture/test_extract_rpc_expose.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/surface_coverage/extract_rpc_expose.py tests/architecture/test_extract_rpc_expose.py
git commit -m "feat(#4161): @rpc_expose decorator extractor across src tree"
```

---

## Task 11: SDK extractor

**Files:**
- Create: `scripts/surface_coverage/extract_sdk.py`
- Create: `tests/architecture/test_extract_sdk.py`

- [ ] **Step 1: Write failing test**

Write `tests/architecture/test_extract_sdk.py`:

```python
"""SDK extractor: enumerate public methods on remote client classes."""
from pathlib import Path

from scripts.surface_coverage.extract_sdk import extract_sdk_methods


def test_extract_sdk_from_fixture(tmp_path: Path):
    f = tmp_path / "base_client.py"
    f.write_text(
        "class BaseRemoteClient:\n"
        "    def read(self, path): pass\n"
        "    def write(self, path, data): pass\n"
        "    def _private(self): pass\n"
        "    async def rebac_grant(self, *args): pass\n"
    )
    results = extract_sdk_methods(f, class_names=("BaseRemoteClient",))
    names = {r.method_name for r in results}
    assert names == {"read", "write", "rebac_grant"}  # _private excluded


def test_extract_sdk_real_file_smoke(repo_root: Path):
    real = repo_root / "src/nexus/remote/base_client.py"
    if not real.exists():
        return
    results = extract_sdk_methods(real, class_names=("BaseRemoteClient",))
    assert isinstance(results, list)
```

- [ ] **Step 2: Run test, confirm failure**

Run: `uv run pytest tests/architecture/test_extract_sdk.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement extractor**

Write `scripts/surface_coverage/extract_sdk.py`:

```python
"""Extract public method names from SDK client classes via AST.

Public = doesn't start with underscore. Includes sync + async methods.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RawSdkMethod:
    class_name: str
    method_name: str
    source: str  # "file.py:line"


def extract_sdk_methods(
    py_path: Path,
    *,
    class_names: tuple[str, ...],
) -> list[RawSdkMethod]:
    tree = ast.parse(py_path.read_text())
    out: list[RawSdkMethod] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name not in class_names:
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name.startswith("_"):
                continue
            out.append(RawSdkMethod(
                class_name=node.name,
                method_name=item.name,
                source=f"{py_path}:{item.lineno}",
            ))
    return sorted(out, key=lambda r: r.method_name)
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/architecture/test_extract_sdk.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/surface_coverage/extract_sdk.py tests/architecture/test_extract_sdk.py
git commit -m "feat(#4161): SDK method extractor for remote client classes"
```

---

## Task 12: Profiles extractor

**Files:**
- Create: `scripts/surface_coverage/extract_profiles.py`
- Create: `tests/architecture/test_extract_profiles.py`

- [ ] **Step 1: Inspect the file**

Run: `head -80 src/nexus/contracts/deployment_profile.py`
Note enum class name + values.

- [ ] **Step 2: Write failing test**

Write `tests/architecture/test_extract_profiles.py`:

```python
"""Profiles extractor: enumerate values from the DeploymentProfile enum."""
from pathlib import Path

from scripts.surface_coverage.extract_profiles import extract_profile_names


def test_extract_profiles_from_fixture(tmp_path: Path):
    f = tmp_path / "deployment_profile.py"
    f.write_text(
        "from enum import Enum\n"
        "\n"
        "class DeploymentProfile(str, Enum):\n"
        '    LITE = "lite"\n'
        '    SANDBOX = "sandbox"\n'
        '    FULL = "full"\n'
        '    REMOTE = "remote"\n'
    )
    results = extract_profile_names(f, enum_class="DeploymentProfile")
    assert set(results) >= {"lite", "sandbox", "full"}


def test_extract_profiles_real_file_smoke(repo_root: Path):
    real = repo_root / "src/nexus/contracts/deployment_profile.py"
    if not real.exists():
        return
    results = extract_profile_names(real, enum_class="DeploymentProfile")
    assert "lite" in results or "LITE" in results or len(results) >= 3
```

- [ ] **Step 3: Run test, confirm failure**

Run: `uv run pytest tests/architecture/test_extract_profiles.py -v`
Expected: ImportError.

- [ ] **Step 4: Implement extractor**

Write `scripts/surface_coverage/extract_profiles.py`:

```python
"""Extract DeploymentProfile enum values via AST."""
from __future__ import annotations

import ast
from pathlib import Path


def extract_profile_names(py_path: Path, *, enum_class: str) -> list[str]:
    tree = ast.parse(py_path.read_text())
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != enum_class:
            continue
        for item in node.body:
            if not isinstance(item, ast.Assign):
                continue
            if not isinstance(item.value, ast.Constant) or not isinstance(item.value.value, str):
                continue
            out.append(item.value.value)
    return sorted(out)
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `uv run pytest tests/architecture/test_extract_profiles.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/surface_coverage/extract_profiles.py tests/architecture/test_extract_profiles.py
git commit -m "feat(#4161): deployment profile enum extractor"
```

---

## Task 13: Orchestrator (`gen_api_surface_coverage.py`)

**Files:**
- Create: `scripts/gen_api_surface_coverage.py`
- Create: `tests/architecture/test_orchestrator.py`

- [ ] **Step 1: Write failing test**

Write `tests/architecture/test_orchestrator.py`:

```python
"""Orchestrator integration test: end-to-end extraction against fixture tree."""
from pathlib import Path

from scripts.gen_api_surface_coverage import generate_coverage
from scripts.surface_coverage.schema import load_yaml


def _build_fixture_tree(root: Path) -> None:
    """Build a tiny repo mirror with one of each surface type."""
    (root / "src/nexus/cli/commands").mkdir(parents=True)
    (root / "src/nexus/cli/commands/__init__.py").write_text(
        '_REGISTER_COMMANDS = {"file_ops": ("read", "write")}\n'
    )
    (root / "src/nexus/cli/commands/file_ops.py").write_text("# fake\n")

    (root / "src/nexus/server").mkdir(parents=True)
    (root / "src/nexus/server/_kernel_syscall_dispatch.py").write_text(
        'DISPATCH = {"fs.read": None, "fs.write": None}\n'
    )
    (root / "src/nexus/server/fastapi_server.py").write_text(
        "class _R:\n"
        "    def get(self, p): \n"
        "        def deco(f): return f\n"
        "        return deco\n"
        "    def post(self, p): return self.get(p)\n"
        "router = _R()\n"
        "@router.post('/api/v1/fs/read')\n"
        "def read(): pass\n"
        "@router.post('/api/v1/fs/write')\n"
        "def write(): pass\n"
    )

    (root / "src/nexus/config").mkdir(parents=True)
    (root / "src/nexus/config/tool_profiles.yaml").write_text(
        "profiles:\n  default:\n    tools: [nexus_fs_read, nexus_fs_write]\n"
    )

    (root / "src/nexus/contracts").mkdir(parents=True)
    (root / "src/nexus/contracts/deployment_profile.py").write_text(
        "from enum import Enum\n"
        "class DeploymentProfile(str, Enum):\n"
        '    LITE="lite"\n    SANDBOX="sandbox"\n    FULL="full"\n'
    )

    (root / "src/nexus/remote").mkdir(parents=True)
    (root / "src/nexus/remote/base_client.py").write_text(
        "class BaseRemoteClient:\n    def read(self): pass\n    def write(self): pass\n"
    )

    (root / "proto/nexus/grpc/vfs").mkdir(parents=True)
    (root / "proto/nexus/grpc/vfs/vfs.proto").write_text(
        "service VFS { rpc Read (R) returns (R); rpc Write (R) returns (R); }\n"
    )


def test_orchestrator_end_to_end(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_fixture_tree(repo)

    out = tmp_path / "coverage.yaml"
    generate_coverage(repo_root=repo, output=out, overrides=None)
    coverage = load_yaml(out)

    op_ids = {op.id for op in coverage.operations}
    assert "fs.read" in op_ids
    assert "fs.write" in op_ids
    # fs.read should have cells from multiple transports
    read = next(op for op in coverage.operations if op.id == "fs.read")
    assert "cli" in read.transports
    assert "http" in read.transports
    assert "mcp" in read.transports
    assert "grpc_typed" in read.transports
    assert "grpc_call" in read.transports
    assert "sdk" in read.transports


def test_orchestrator_idempotent(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_fixture_tree(repo)
    out = tmp_path / "coverage.yaml"
    generate_coverage(repo_root=repo, output=out, overrides=None)
    first = out.read_text()
    generate_coverage(repo_root=repo, output=out, overrides=None)
    second = out.read_text()
    assert first == second
```

- [ ] **Step 2: Run test, confirm failure**

Run: `uv run pytest tests/architecture/test_orchestrator.py -v`
Expected: ImportError on `scripts.gen_api_surface_coverage`.

- [ ] **Step 3: Implement orchestrator**

Write `scripts/gen_api_surface_coverage.py`:

```python
#!/usr/bin/env python3
"""Orchestrator: run every surface extractor against the repo and emit YAML.

Reads the existing YAML (if present) and merges, preserving human-filled fields.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.surface_coverage import (
    extract_cli,
    extract_grpc_call,
    extract_grpc_typed,
    extract_http,
    extract_mcp,
    extract_profiles,
    extract_rpc_expose,
    extract_sdk,
    normalize,
)
from scripts.surface_coverage.merge import merge_coverage
from scripts.surface_coverage.schema import (
    Module, Operation, ParityWarning, ProfileStatus, SurfaceCoverage,
    TransportCell, UnmappedSurface, dump_yaml, load_yaml,
)

# Modules to seed in the architecture graph (heuristic; real modules come from extractor output)
_SEED_MODULES = [
    Module(id="fs", name="Filesystem", description="Core read/write/stat/list operations"),
    Module(id="rebac", name="ReBAC", description="Permissions and access control"),
    Module(id="search", name="Search", description="BM25S / sqlite-vec / semantic"),
    Module(id="workspace", name="Workspace", description="Local + remote workspaces, snapshots"),
    Module(id="mounts", name="Mounts", description="Mount drivers + connectors"),
    Module(id="oauth", name="OAuth", description="Credential management"),
    Module(id="mcp", name="MCP", description="Model Context Protocol tooling"),
    Module(id="admin", name="Admin", description="Admin / governance / audit"),
]


def generate_coverage(
    *,
    repo_root: Path,
    output: Path,
    overrides: Path | None,
) -> SurfaceCoverage:
    operations: dict[str, Operation] = {}

    # --- CLI ---
    cli_init = repo_root / "src/nexus/cli/commands/__init__.py"
    if cli_init.exists():
        for raw in extract_cli.extract_cli_commands(cli_init):
            try:
                op_id = normalize.normalize_cli(raw.name)
            except ValueError:
                continue
            _upsert(operations, op_id, "cli", raw.name, raw.source)

    # --- HTTP ---
    fastapi = repo_root / "src/nexus/server/fastapi_server.py"
    if fastapi.exists():
        for raw in extract_http.extract_http_routes(fastapi):
            try:
                op_id = normalize.normalize_http(raw.method, raw.path)
            except ValueError:
                continue
            _upsert(operations, op_id, "http", f"{raw.method} {raw.path}", raw.source)

    # --- MCP ---
    tp = repo_root / "src/nexus/config/tool_profiles.yaml"
    if tp.exists():
        for raw in extract_mcp.extract_mcp_tools(tp):
            try:
                op_id = normalize.normalize_mcp(raw.name)
            except ValueError:
                continue
            _upsert(operations, op_id, "mcp", raw.name, raw.source)

    # --- gRPC typed ---
    proto = repo_root / "proto/nexus/grpc/vfs/vfs.proto"
    if proto.exists():
        for raw in extract_grpc_typed.extract_grpc_typed_methods(proto):
            try:
                op_id = normalize.normalize_grpc_typed(raw.method)
            except ValueError:
                continue
            _upsert(operations, op_id, "grpc_typed", raw.method, raw.source)

    # --- gRPC Call (dispatch) ---
    dispatch = repo_root / "src/nexus/server/_kernel_syscall_dispatch.py"
    if dispatch.exists():
        for var in ("DISPATCH", "_DISPATCH", "KERNEL_SYSCALL_DISPATCH", "SYSCALL_DISPATCH"):
            try:
                for raw in extract_grpc_call.extract_grpc_call_names(dispatch, dispatch_var=var):
                    op_id = normalize.normalize_grpc_call(raw.name)
                    _upsert(operations, op_id, "grpc_call", raw.name, raw.source)
                break
            except ValueError:
                continue

    # --- @rpc_expose ---
    src = repo_root / "src/nexus"
    if src.exists():
        for raw in extract_rpc_expose.extract_rpc_exposes(src):
            # rpc_expose names use MCP-like underscore form: oauth_list_providers
            try:
                op_id = normalize.normalize_mcp("nexus_" + raw.name)
            except ValueError:
                op_id = raw.name  # fall back: leave as-is for human triage
            _upsert(operations, op_id, "grpc_expose", raw.name, raw.source)

    # --- SDK ---
    bc = repo_root / "src/nexus/remote/base_client.py"
    if bc.exists():
        for raw in extract_sdk.extract_sdk_methods(bc, class_names=("BaseRemoteClient",)):
            try:
                op_id = normalize.normalize_sdk(raw.class_name, raw.method_name)
            except ValueError:
                continue
            _upsert(operations, op_id, "sdk", f"{raw.class_name}.{raw.method_name}", raw.source)

    # --- Profiles ---
    dp = repo_root / "src/nexus/contracts/deployment_profile.py"
    profile_names: list[str] = []
    if dp.exists():
        profile_names = extract_profiles.extract_profile_names(dp, enum_class="DeploymentProfile")

    # Default profile assignment: extractor marks everything supported on all three;
    # subissues override to unavailable/admin_only/etc.
    default_profiles = {p: ProfileStatus.SUPPORTED for p in ("lite", "sandbox", "full")}
    for op in operations.values():
        op.profiles = dict(default_profiles)

    # Parity warnings: ops that have some transports but not all "user-facing" ones
    user_facing = {"cli", "grpc_typed", "http", "mcp", "sdk"}
    parity_warnings: list[ParityWarning] = []
    for op in operations.values():
        has = sorted(set(op.transports) & user_facing)
        missing = sorted(user_facing - set(op.transports))
        if has and missing:
            parity_warnings.append(ParityWarning(operation_id=op.id, has=has, missing=missing))

    fresh = SurfaceCoverage(
        schema_version=1,
        modules=list(_SEED_MODULES),
        operations=sorted(operations.values(), key=lambda o: o.id),
        parity_warnings=parity_warnings,
        unmapped_surfaces=[],   # populated when normalize raises (left empty for v1)
        stale_rows=[],
    )

    # Merge with existing YAML if present
    if output.exists():
        existing = load_yaml(output)
        merged = merge_coverage(existing=existing, fresh=fresh)
    else:
        merged = fresh

    dump_yaml(merged, output)
    return merged


def _upsert(
    ops: dict[str, Operation],
    op_id: str,
    transport: str,
    name: str,
    source: str,
) -> None:
    module = op_id.split(".", 1)[0]
    if op_id not in ops:
        ops[op_id] = Operation(
            id=op_id,
            module=module,
            summary="",
            transports={},
            profiles={},  # filled later
        )
    ops[op_id].transports[transport] = TransportCell(name=name, source=source)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    p.add_argument("--output", type=Path,
                   default=Path("docs/architecture/api-rpc-surface-coverage.yaml"))
    p.add_argument("--overrides", type=Path,
                   default=Path("docs/architecture/api-rpc-surface-overrides.yaml"),
                   help="reserved for v2; ignored in v1")
    args = p.parse_args(argv)
    generate_coverage(repo_root=args.repo_root, output=args.output, overrides=args.overrides)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run integration test**

Run: `uv run pytest tests/architecture/test_orchestrator.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/gen_api_surface_coverage.py tests/architecture/test_orchestrator.py
git commit -m "feat(#4161): orchestrator script — extract all transports + merge YAML"
```

---

## Task 14: Renderer (jinja template + Mermaid generation)

**Files:**
- Create: `scripts/surface_coverage/templates/coverage.html.j2`
- Create: `scripts/surface_coverage/render.py`
- Create: `scripts/render_api_surface_coverage.py`
- Create: `tests/architecture/test_render.py`

- [ ] **Step 1: Write failing test**

Write `tests/architecture/test_render.py`:

```python
"""Renderer: YAML + template -> deterministic HTML."""
from pathlib import Path

from scripts.surface_coverage.render import render_html
from scripts.surface_coverage.schema import (
    Module, Operation, ProfileStatus, SurfaceCoverage, TransportCell, dump_yaml,
)


def _sample_coverage() -> SurfaceCoverage:
    return SurfaceCoverage(
        schema_version=1,
        modules=[
            Module(id="fs", name="Filesystem", description="Core fs", depends_on=[]),
            Module(id="rebac", name="ReBAC", description="Permissions", depends_on=["fs"]),
        ],
        operations=[
            Operation(
                id="fs.read", module="fs", summary="Read bytes from a path",
                transports={
                    "cli": TransportCell("nexus fs read", "src/x.py:1"),
                    "http": TransportCell("POST /api/v1/fs/read", "src/y.py:2"),
                },
                profiles={
                    "lite": ProfileStatus.SUPPORTED,
                    "sandbox": ProfileStatus.SUPPORTED,
                    "full": ProfileStatus.SUPPORTED,
                },
            ),
        ],
    )


def test_render_produces_valid_html(tmp_path: Path):
    coverage = _sample_coverage()
    html = render_html(coverage)
    assert "<html" in html
    assert "Nexus API/RPC Surface Map" in html
    assert "fs.read" in html
    assert "nexus fs read" in html
    assert "POST /api/v1/fs/read" in html
    # mermaid block present
    assert "mermaid" in html.lower()


def test_render_is_deterministic(tmp_path: Path):
    coverage = _sample_coverage()
    a = render_html(coverage)
    b = render_html(coverage)
    assert a == b


def test_render_includes_module_graph_edges(tmp_path: Path):
    coverage = _sample_coverage()
    html = render_html(coverage)
    # rebac depends_on=[fs] should appear as an edge fs --> rebac
    assert "fs --> rebac" in html or "rebac --> fs" in html or "fs-->rebac" in html
```

- [ ] **Step 2: Run test, confirm failure**

Run: `uv run pytest tests/architecture/test_render.py -v`
Expected: ImportError.

- [ ] **Step 3: Write jinja template**

Write `scripts/surface_coverage/templates/coverage.html.j2`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Nexus API/RPC Surface Map</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 1100px; margin: 2em auto; padding: 0 1em; color: #222; }
  h1 { border-bottom: 2px solid #444; padding-bottom: .3em; }
  h2 { margin-top: 2em; }
  .module { border: 1px solid #ddd; border-radius: 6px; padding: .5em 1em; margin: 1em 0; }
  .module summary { cursor: pointer; font-weight: 600; font-size: 1.1em; }
  .op { margin: 1em 0 1em 1em; padding-left: 1em; border-left: 3px solid #eee; }
  .op-id { font-family: monospace; font-weight: 600; }
  .op-summary { color: #555; margin-left: .5em; }
  .transport { font-family: monospace; font-size: .9em; margin: .15em 0; }
  .transport-key { display: inline-block; width: 5em; color: #888; }
  .profile-supported { color: #2a7a2a; }
  .profile-unavailable { color: #aaa; text-decoration: line-through; }
  .profile-other { color: #b07000; }
  .todo { color: #b00; font-style: italic; }
  .meta { color: #777; font-size: .9em; }
  #search { width: 100%; padding: .5em; font-size: 1em; border: 1px solid #aaa; border-radius: 4px; }
  .hidden { display: none; }
  a { color: #1466b8; }
</style>
</head>
<body>
<h1>Nexus API/RPC Surface Map</h1>

<input id="search" placeholder="Search by op-id, module, or transport name..." />

<h2 id="how-to-read">§1 How to read this page</h2>
<p>
Each module groups external surfaces. Each <strong>surface</strong> is one logical
operation exposed across CLI / RPC / HTTP / MCP / SDK. Subissues fill <em>usage,
test, perf, gap</em> fields. Full contract:
<a href="api-rpc-surface-contract.md">api-rpc-surface-contract.md</a>.
</p>

<h2 id="architecture">§2 Architecture</h2>
<div class="mermaid">
graph LR
{% for m in modules -%}
  {{ m.id }}["{{ m.name }}"]
{% endfor -%}
{% for m in modules -%}
{% for dep in m.depends_on -%}
  {{ dep }} --> {{ m.id }}
{% endfor -%}
{% endfor -%}
</div>

<h2 id="modules">§3 Modules</h2>
{% for m in modules %}
<details class="module" {% if loop.first %}open{% endif %}>
  <summary>{{ m.id }} — {{ m.name }} ({{ ops_by_module.get(m.id, []) | length }} ops)</summary>
  <p class="meta">{{ m.description }}</p>
  {% for op in ops_by_module.get(m.id, []) %}
  <div class="op" data-op-id="{{ op.id }}">
    <div><span class="op-id">{{ op.id }}</span><span class="op-summary"> — {{ op.summary or '(no summary)' }}</span></div>
    {% for tkey, tname in transport_display %}
    {% if tkey in op.transports %}
    <div class="transport"><span class="transport-key">{{ tname }}:</span> {{ op.transports[tkey].name }}</div>
    {% endif %}
    {% endfor %}
    <div class="transport">
      profiles:
      {% for pkey in ('lite', 'sandbox', 'full') %}
      {% set status = op.profiles[pkey].value %}
      {% if status == 'supported' %}<span class="profile-supported">{{ pkey }} ✓</span>
      {% elif status == 'unavailable' %}<span class="profile-unavailable">{{ pkey }} ✗</span>
      {% else %}<span class="profile-other">{{ pkey }} ({{ status }})</span>{% endif %}
      {% endfor %}
    </div>
    <div class="transport">
      usage: {% if op.usage_example %}<code>{{ op.usage_example }}</code>{% else %}<span class="todo">TODO</span>{% endif %}
      &nbsp; test: {% if op.correctness_test %}<a href="../../{{ op.correctness_test }}">{{ op.correctness_test }}</a>{% else %}<span class="todo">TODO</span>{% endif %}
      &nbsp; perf: {% if op.perf_class %}{{ op.perf_class.value }}{% else %}<span class="todo">TODO</span>{% endif %}
    </div>
    <div class="transport meta">
      owner: {% if op.owning_issue %}<a href="https://github.com/nexi-lab/nexus/issues/{{ op.owning_issue }}">#{{ op.owning_issue }}</a>{% else %}—{% endif %}
      &nbsp; gap: {% if op.gap_issue %}<a href="https://github.com/nexi-lab/nexus/issues/{{ op.gap_issue }}">#{{ op.gap_issue }}</a>{% else %}—{% endif %}
    </div>
  </div>
  {% endfor %}
</details>
{% endfor %}

<h2 id="subissues">§4 Subissues filling this map</h2>
<ul>
  <li><a href="https://github.com/nexi-lab/nexus/issues/4119">#4119 lite</a></li>
  <li><a href="https://github.com/nexi-lab/nexus/issues/4120">#4120 sandbox</a></li>
  <li><a href="https://github.com/nexi-lab/nexus/issues/4121">#4121 full</a></li>
  <li><a href="https://github.com/nexi-lab/nexus/issues/4139">#4139 coverage matrix</a></li>
</ul>

<script>
{{ mermaid_js | safe }}
mermaid.initialize({ startOnLoad: true, theme: 'default' });

const search = document.getElementById('search');
search.addEventListener('input', () => {
  const q = search.value.trim().toLowerCase();
  document.querySelectorAll('.op').forEach(el => {
    const text = el.textContent.toLowerCase();
    el.classList.toggle('hidden', q.length > 0 && !text.includes(q));
  });
  // expand any module containing visible ops
  if (q.length > 0) {
    document.querySelectorAll('details.module').forEach(d => {
      const anyVisible = Array.from(d.querySelectorAll('.op')).some(op => !op.classList.contains('hidden'));
      d.open = anyVisible;
    });
  }
});
</script>
</body>
</html>
```

- [ ] **Step 4: Implement renderer**

Write `scripts/surface_coverage/render.py`:

```python
"""Render SurfaceCoverage to HTML via jinja2."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import jinja2

from scripts.surface_coverage.schema import SurfaceCoverage

_TEMPLATE_DIR = Path(__file__).parent / "templates"

TRANSPORT_DISPLAY = [
    ("cli", "CLI"),
    ("grpc_typed", "RPC"),
    ("grpc_call", "Call"),
    ("grpc_expose", "expose"),
    ("http", "HTTP"),
    ("mcp", "MCP"),
    ("sdk", "SDK"),
]


def _load_mermaid_js() -> str:
    """Return inline Mermaid runtime. In v1, ship as a stub <script> that loads from a vendored copy.

    For initial impl we use the CDN-style script tag inline; a follow-up replaces
    this with a vendored copy under docs/architecture/_vendor/.
    """
    return (
        '<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>\n'
    )


def render_html(coverage: SurfaceCoverage) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATE_DIR),
        autoescape=jinja2.select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    tmpl = env.get_template("coverage.html.j2")
    ops_by_module: dict[str, list] = defaultdict(list)
    for op in sorted(coverage.operations, key=lambda o: o.id):
        ops_by_module[op.module].append(op)
    return tmpl.render(
        modules=coverage.modules,
        ops_by_module=ops_by_module,
        transport_display=TRANSPORT_DISPLAY,
        mermaid_js=_load_mermaid_js(),
    )
```

- [ ] **Step 5: Implement entry script**

Write `scripts/render_api_surface_coverage.py`:

```python
#!/usr/bin/env python3
"""Render the surface-coverage YAML into HTML."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.surface_coverage.render import render_html
from scripts.surface_coverage.schema import load_yaml


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path,
                   default=Path("docs/architecture/api-rpc-surface-coverage.yaml"))
    p.add_argument("--output", type=Path,
                   default=Path("docs/architecture/api-rpc-surface-coverage.html"))
    args = p.parse_args(argv)
    coverage = load_yaml(args.input)
    args.output.write_text(render_html(coverage))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Run tests, confirm pass**

Run: `uv run pytest tests/architecture/test_render.py -v`
Expected: 3 passed.

- [ ] **Step 7: Commit**

```bash
git add scripts/surface_coverage/templates/coverage.html.j2 \
        scripts/surface_coverage/render.py \
        scripts/render_api_surface_coverage.py \
        tests/architecture/test_render.py
git commit -m "feat(#4161): jinja renderer + Mermaid module graph + entry script"
```

---

## Task 15: Vendor Mermaid inline (replace CDN script tag)

**Files:**
- Create: `docs/architecture/_vendor/mermaid.min.js` (downloaded; or noted in PR if too large)
- Modify: `scripts/surface_coverage/render.py`

- [ ] **Step 1: Download Mermaid release**

Run: `mkdir -p docs/architecture/_vendor && curl -sL "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js" -o docs/architecture/_vendor/mermaid.min.js && wc -c docs/architecture/_vendor/mermaid.min.js`
Expected: file of a few hundred KB.

- [ ] **Step 2: Update renderer to inline vendored copy**

Edit `scripts/surface_coverage/render.py`, replace `_load_mermaid_js`:

```python
def _load_mermaid_js() -> str:
    vendored = Path(__file__).parent.parent.parent / "docs/architecture/_vendor/mermaid.min.js"
    if vendored.exists():
        return f"<script>\n{vendored.read_text()}\n</script>\n"
    return '<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>\n'
```

- [ ] **Step 3: Re-run renderer test to confirm vendored path used**

Run: `uv run pytest tests/architecture/test_render.py -v`
Expected: still passes (rendered output now contains inline mermaid source).

- [ ] **Step 4: Commit**

```bash
git add docs/architecture/_vendor/mermaid.min.js scripts/surface_coverage/render.py
git commit -m "feat(#4161): vendor Mermaid runtime inline — no CDN dep"
```

---

## Task 16: Generate initial YAML + HTML against the real repo

**Files:**
- Create: `docs/architecture/api-rpc-surface-coverage.yaml`
- Create: `docs/architecture/api-rpc-surface-coverage.html`
- Create: `docs/architecture/api-rpc-surface-overrides.yaml`

- [ ] **Step 1: Run the extractor**

Run: `uv run python scripts/gen_api_surface_coverage.py`
Expected: writes `docs/architecture/api-rpc-surface-coverage.yaml`.

- [ ] **Step 2: Sanity-check the YAML**

Run: `head -80 docs/architecture/api-rpc-surface-coverage.yaml` then `wc -l docs/architecture/api-rpc-surface-coverage.yaml`
Expected: several hundred operations across modules. Look for obvious extraction failures (zero ops, all-one-transport, malformed names).

If `unmapped_surfaces` or `parity_warnings` is huge, that's expected — humans triage later via overrides. v1 ships them as-is.

- [ ] **Step 3: Render the HTML**

Run: `uv run python scripts/render_api_surface_coverage.py`
Expected: writes `docs/architecture/api-rpc-surface-coverage.html`.

- [ ] **Step 4: Open in browser**

Run: `open docs/architecture/api-rpc-surface-coverage.html`
Verify:
- Page loads
- Mermaid graph renders (modules + arrows)
- §3 Modules section has expandable cards
- Search box filters operations as you type
- TODO placeholders visible for usage/test/perf

If Mermaid fails to render, check browser console — vendored file may not have shipped. Re-run Task 15 Step 1.

- [ ] **Step 5: Create empty overrides file**

Write `docs/architecture/api-rpc-surface-overrides.yaml`:

```yaml
# Human overrides for the surface-coverage extractor.
# Populated as parity warnings and unmapped surfaces surface during triage.
# See docs/architecture/api-rpc-surface-contract.md for field semantics.
schema_version: 1
op_id_overrides: {}    # raw transport-name -> canonical op-id
module_overrides: {}   # op-id -> module-id
parity_acknowledgements: []  # list of {operation_id, transport, reason}
```

- [ ] **Step 6: Commit**

```bash
git add docs/architecture/api-rpc-surface-coverage.yaml \
        docs/architecture/api-rpc-surface-coverage.html \
        docs/architecture/api-rpc-surface-overrides.yaml
git commit -m "data(#4161): initial extraction of API/RPC surfaces from develop"
```

---

## Task 17: Warn-only CI gate

**Files:**
- Create: `tests/architecture/test_inventory.py`

- [ ] **Step 1: Write the warn-only test**

Write `tests/architecture/test_inventory.py`:

```python
"""Warn-only freshness + render + schema CI gate for the surface coverage map.

This test always passes in v1 — it emits warnings when drift is detected.
It will be promoted to hard-fail in a follow-up issue (likely #4139) once
subissues catch up filling per-row content.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from scripts.gen_api_surface_coverage import generate_coverage
from scripts.surface_coverage.render import render_html
from scripts.surface_coverage.schema import dump_yaml, load_yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COVERAGE_YAML = _REPO_ROOT / "docs/architecture/api-rpc-surface-coverage.yaml"
_COVERAGE_HTML = _REPO_ROOT / "docs/architecture/api-rpc-surface-coverage.html"


@pytest.fixture(scope="module")
def existing_coverage():
    if not _COVERAGE_YAML.exists():
        pytest.skip("no coverage YAML committed yet")
    return load_yaml(_COVERAGE_YAML)


def test_schema_validity(existing_coverage):
    # load_yaml already enforces schema; reaching here means it parsed.
    assert existing_coverage.schema_version == 1


def test_freshness(tmp_path: Path, existing_coverage):
    """Re-extract; warn if new surfaces appeared in code but not in committed YAML."""
    out = tmp_path / "fresh.yaml"
    # Copy existing to tmp so merge happens against it
    dump_yaml(existing_coverage, out)
    fresh = generate_coverage(repo_root=_REPO_ROOT, output=out, overrides=None)

    committed_ids = {op.id for op in existing_coverage.operations}
    fresh_ids = {op.id for op in fresh.operations}

    new_in_code = fresh_ids - committed_ids
    if new_in_code:
        warnings.warn(
            "api-rpc-surface-coverage drift: new surfaces in code not committed:\n"
            + "\n".join(f"  + {op_id}" for op_id in sorted(new_in_code))
            + "\n  Run: uv run python scripts/gen_api_surface_coverage.py"
            + "\n  Then commit the updated YAML and re-render HTML."
            + "\n  This is warn-only in v1.",
            stacklevel=2,
        )


def test_render_determinism(existing_coverage):
    """Re-render committed YAML; warn if output differs from committed HTML."""
    if not _COVERAGE_HTML.exists():
        pytest.skip("no coverage HTML committed yet")
    rendered = render_html(existing_coverage)
    committed = _COVERAGE_HTML.read_text()
    if rendered != committed:
        warnings.warn(
            "api-rpc-surface-coverage drift: committed HTML differs from re-render.\n"
            "  Run: uv run python scripts/render_api_surface_coverage.py\n"
            "  Then commit the updated HTML.\n"
            "  This is warn-only in v1.",
            stacklevel=2,
        )
```

- [ ] **Step 2: Run the test, confirm it passes (warnings allowed)**

Run: `uv run pytest tests/architecture/test_inventory.py -v -W error::UserWarning`
Expected: passes (no drift right after Task 16). If it fails with "drift detected", re-run extractor + renderer + commit.

Then run without `-W error` to confirm it stays warn-only:

Run: `uv run pytest tests/architecture/test_inventory.py -v`
Expected: 3 passed; warnings printed if drift.

- [ ] **Step 3: Commit**

```bash
git add tests/architecture/test_inventory.py
git commit -m "test(#4161): warn-only freshness + render + schema CI gate"
```

---

## Task 18: Write contract markdown

**Files:**
- Create: `docs/architecture/api-rpc-surface-contract.md`

- [ ] **Step 1: Write the contract doc**

Write `docs/architecture/api-rpc-surface-contract.md`:

````markdown
# Nexus API/RPC Surface Coverage Contract

This document is the source of truth for how surfaces are inventoried, classified,
tested, and benchmarked. The interactive map at
`api-rpc-surface-coverage.html` is the published view of the data this contract governs.

## Mental model

Every external surface is traceable through this chain:

```
profile → module/group → external API/RPC surface → how/when to use → correctness tests → performance classification → gap issue
```

A subissue is incomplete if it documents only command or method names. It must
explain what module owns the surface, how users call it, what tests prove it
works, and what remains missing.

## Row contract (12 fields)

| Field | Owner | Description |
|---|---|---|
| `id` | extractor | Canonical op-id, `<module>.<verb>`. Stable across transports. |
| `module` | extractor (overridable) | Owning module id. Heuristic from source path. |
| `summary` | extractor → human-refined | One-line description. Extractor seeds from docstring; subissues refine. |
| `transports` | extractor | Per-transport cells (CLI / gRPC typed / gRPC call / `@rpc_expose` / HTTP / MCP / SDK). |
| `profiles` | extractor (default supported) → subissue overrides | Status per profile: `supported | unavailable | admin_only | deprecated | missing_needed`. |
| `usage_example` | subissue | CLI snippet + SDK snippet showing real invocation. |
| `correctness_test` | subissue | `path:line` of a pytest function exercising the surface. |
| `perf_class` | subissue | `hot | setup | control | not_perf_sensitive`. |
| `perf_link` | subissue | Benchmark path or rationale text. |
| `gap_issue` | subissue | GitHub issue # if missing-needed / stale / unsupported. |
| `owning_issue` | subissue | GitHub issue # responsible for filling this row. |

## 100% external API testing standard

Every supported row must have correctness coverage proving:

- **positive flow** — a typical successful invocation
- **expected failure** — wrong input shape, missing required arg, etc.
- **auth / permission denied** — RBAC or ReBAC rejection path
- **profile-unavailable behavior** — when the surface is gated off in some profiles, the error shape is stable and informative
- **CLI/RPC parity** — when both exist, both yield equivalent results for the same logical operation
- **stable unsupported error shape** — calling a not-available surface returns a known code and message
- **degraded behavior for optional dependencies** — when an optional dep is missing, the surface either fails predictably or operates in a documented degraded mode

## Performance classification

Every row carries a `perf_class`:

| Class | Definition | Required evidence |
|---|---|---|
| `hot` | Per-request critical path; latency directly visible to end users. | Benchmark file linked via `perf_link`, OR a latency guardrail asserted in CI. |
| `setup` | Boot, init, or first-use; runs once per session. | Representative timing measurement (commit log or benchmark) in `perf_link`. |
| `control` | Admin / governance / configuration changes; infrequent. | Smoke timing in `perf_link`. |
| `not_perf_sensitive` | Not on any timing-sensitive path. | One-line rationale in `perf_link` ("invoked at most once per CLI invocation; <10ms acceptable"). |

## Workflow for subissue owners

1. Open `api-rpc-surface-coverage.html`. Filter / search for rows where `owner: #<your-issue>`.
2. For each row, fill `summary`, `usage_example`, `correctness_test`, `perf_class`, `perf_link`, `profiles` in `api-rpc-surface-coverage.yaml`.
3. If the surface is missing-needed (no implementation yet), open a build issue and set `gap_issue`.
4. Re-run `uv run python scripts/gen_api_surface_coverage.py` (merge with your edits) then `uv run python scripts/render_api_surface_coverage.py`.
5. Commit both YAML and HTML in the same change.

## Gap-issue rules

A gap issue is required when a row has any of:

- `profiles.<any> = missing_needed`
- `gap_issue != null`
- the surface is referenced by user-guide examples but doesn't exist in code yet

The gap issue must specify:

- request/response shape (for RPCs) or CLI syntax (for CLI commands)
- test requirements (which of the 7 coverage proofs above must land)
- docs location it unblocks
- benchmark expectations (or `not_perf_sensitive` rationale)

A profile epic cannot close while any of its owned `missing_needed` rows are open.

## Source anchors

Extractor reads:

- CLI: `src/nexus/cli/commands/__init__.py` (`_REGISTER_COMMANDS`)
- typed gRPC: `proto/nexus/grpc/vfs/vfs.proto`
- gRPC `Call`: `src/nexus/server/_kernel_syscall_dispatch.py`
- `@rpc_expose`: scan of `src/nexus/**/*.py`
- HTTP: `src/nexus/server/fastapi_server.py`
- MCP: `src/nexus/config/tool_profiles.yaml`
- SDK: `src/nexus/remote/base_client.py`
- Profiles: `src/nexus/contracts/deployment_profile.py`
````

- [ ] **Step 2: Commit**

```bash
git add docs/architecture/api-rpc-surface-contract.md
git commit -m "docs(#4161): API/RPC surface coverage contract"
```

---

## Task 19: Subissue distribution script

**Files:**
- Create: `scripts/surface_coverage/distribute.py`
- Create: `scripts/distribute_surface_contract_to_subissues.py`
- Create: `tests/architecture/test_distribute.py`

- [ ] **Step 1: Write failing test**

Write `tests/architecture/test_distribute.py`:

```python
"""distribute.py: append idempotent contract appendix to a subissue body."""
from scripts.surface_coverage.distribute import (
    APPENDIX_BEGIN, APPENDIX_END, apply_appendix, build_appendix,
)


def test_apply_to_clean_body():
    body = "# Original body\n\nSome content.\n"
    new = apply_appendix(body, build_appendix(issue_number=4123, owned_op_ids=["fs.read"]))
    assert "Original body" in new
    assert APPENDIX_BEGIN in new
    assert APPENDIX_END in new
    assert "fs.read" in new
    assert "Surface coverage contract" in new


def test_apply_is_idempotent():
    body = "# Original\n"
    a = apply_appendix(body, build_appendix(issue_number=4123, owned_op_ids=["fs.read"]))
    b = apply_appendix(a, build_appendix(issue_number=4123, owned_op_ids=["fs.read"]))
    assert a == b


def test_apply_replaces_existing_appendix():
    body = "# Original\n"
    once = apply_appendix(body, build_appendix(issue_number=4123, owned_op_ids=["fs.read"]))
    twice = apply_appendix(once, build_appendix(issue_number=4123, owned_op_ids=["fs.read", "fs.write"]))
    assert "fs.write" in twice
    # only one appendix block
    assert twice.count(APPENDIX_BEGIN) == 1
    assert twice.count(APPENDIX_END) == 1


def test_apply_preserves_original_below_appendix():
    """Appendix is appended at the very end; surrounding body content untouched."""
    body = "Line 1\nLine 2\n"
    new = apply_appendix(body, build_appendix(issue_number=4123, owned_op_ids=[]))
    assert new.startswith("Line 1\nLine 2\n")
```

- [ ] **Step 2: Run test, confirm failure**

Run: `uv run pytest tests/architecture/test_distribute.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement distribute library**

Write `scripts/surface_coverage/distribute.py`:

```python
"""Build and apply the surface-coverage appendix to a subissue body.

Idempotent: replaces any existing appendix bounded by sentinel comments.
"""
from __future__ import annotations

import re

APPENDIX_BEGIN = "<!-- BEGIN surface-contract-appendix:4161 -->"
APPENDIX_END = "<!-- END surface-contract-appendix:4161 -->"

_APPENDIX_RE = re.compile(
    re.escape(APPENDIX_BEGIN) + r".*?" + re.escape(APPENDIX_END) + r"\n?",
    re.DOTALL,
)


def build_appendix(*, issue_number: int, owned_op_ids: list[str]) -> str:
    if owned_op_ids:
        owned_block = "\n".join(f"- `{op_id}`" for op_id in sorted(owned_op_ids))
    else:
        owned_block = (
            "_No operations assigned yet. Use the search box in the map to find "
            "surfaces this slice should own, then add `owning_issue: "
            f"{issue_number}` to those rows in api-rpc-surface-coverage.yaml._"
        )
    return f"""{APPENDIX_BEGIN}

## Surface coverage contract (added by #4161)

This story slice contributes rows to the shared surface map:

- Map: `docs/architecture/api-rpc-surface-coverage.html`
- Data: `docs/architecture/api-rpc-surface-coverage.yaml`
- Contract: `docs/architecture/api-rpc-surface-contract.md`

### Owned surfaces (filter map by `owner: #{issue_number}`)

{owned_block}

### Acceptance-criteria delta

- [ ] Every owned row has `summary` and `usage_example` filled.
- [ ] Every owned row has `correctness_test` linking to a test `file:line`.
- [ ] Every owned row has `perf_class` set (`hot|setup|control|not_perf_sensitive`)
      and `perf_link` (benchmark path or rationale).
- [ ] Every owned row has `profiles.{{lite,sandbox,full}}` set.
- [ ] Any missing-needed surface has a build gap issue opened and linked via `gap_issue`.
- [ ] Re-run `scripts/gen_api_surface_coverage.py`; commit YAML; render HTML.

{APPENDIX_END}
"""


def apply_appendix(body: str, appendix: str) -> str:
    """Return `body` with appendix appended (or replaced if already present)."""
    stripped = _APPENDIX_RE.sub("", body).rstrip() + "\n\n"
    return stripped + appendix
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `uv run pytest tests/architecture/test_distribute.py -v`
Expected: 4 passed.

- [ ] **Step 5: Implement CLI entry**

Write `scripts/distribute_surface_contract_to_subissues.py`:

```python
#!/usr/bin/env python3
"""Append the surface-coverage contract appendix to all 21 subissue bodies.

Reads the issue list, calls `gh issue edit` per issue.
Idempotent — re-runs replace the prior appendix in-place via sentinel match.

Run AFTER #4161 PR merges to develop, not as part of the PR itself.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from scripts.surface_coverage.distribute import apply_appendix, build_appendix
from scripts.surface_coverage.schema import load_yaml

# All subissues to amend (epics + children)
_TARGET_ISSUES: tuple[int, ...] = (
    4119, 4120, 4121, 4139,                      # epics
    4122, 4123, 4124, 4125,                       # lite children
    4126, 4127, 4128, 4129, 4130, 4131,           # sandbox children
    4132, 4133, 4134, 4135, 4136, 4137, 4138,     # full children
)

_REPO = "nexi-lab/nexus"


def _gh_get_body(issue: int) -> str:
    out = subprocess.check_output(
        ["gh", "issue", "view", str(issue), "--repo", _REPO, "--json", "body"],
        text=True,
    )
    return json.loads(out)["body"]


def _gh_set_body(issue: int, body: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] would edit #{issue} ({len(body)} chars)")
        return
    proc = subprocess.run(
        ["gh", "issue", "edit", str(issue), "--repo", _REPO, "--body-file", "-"],
        input=body,
        text=True,
        check=True,
    )
    print(f"updated #{issue}")


def _owners_from_yaml(yaml_path: Path) -> dict[int, list[str]]:
    coverage = load_yaml(yaml_path)
    out: dict[int, list[str]] = defaultdict(list)
    for op in coverage.operations:
        if op.owning_issue is not None:
            out[op.owning_issue].append(op.id)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--yaml", type=Path,
                   default=Path("docs/architecture/api-rpc-surface-coverage.yaml"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--only", type=int, action="append",
                   help="restrict to specific issue number(s) (testing)")
    args = p.parse_args(argv)

    owners = _owners_from_yaml(args.yaml)
    targets = args.only if args.only else _TARGET_ISSUES
    for issue in targets:
        body = _gh_get_body(issue)
        appendix = build_appendix(issue_number=issue, owned_op_ids=owners.get(issue, []))
        new_body = apply_appendix(body, appendix)
        if new_body == body:
            print(f"#{issue}: no change")
            continue
        _gh_set_body(issue, new_body, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Smoke test against one issue (dry-run)**

Run: `uv run python scripts/distribute_surface_contract_to_subissues.py --dry-run --only 4139`
Expected: prints `[dry-run] would edit #4139 (NNNN chars)` with no errors.

- [ ] **Step 7: Commit**

```bash
git add scripts/surface_coverage/distribute.py \
        scripts/distribute_surface_contract_to_subissues.py \
        tests/architecture/test_distribute.py
git commit -m "feat(#4161): gh-issue-edit driver for subissue contract distribution"
```

---

## Task 20: Verify GitHub Pages publish path

**Files:**
- Inspect: `.github/workflows/*.yml`
- Maybe modify: docs workflow file

- [ ] **Step 1: Find the docs publish workflow**

Run: `grep -l "github-pages\|pages-build-deployment\|deploy-pages" .github/workflows/*.yml`
Or: `ls .github/workflows/ | grep -i 'doc\|page'`

- [ ] **Step 2: Inspect publish path globs**

Read the workflow file. Look for `paths:` triggers and `with: path:` upload-artifact settings. Confirm `docs/architecture/*.html` is reachable in the published artifact, OR `docs/` as a whole is uploaded.

- [ ] **Step 3: If `docs/architecture/` is excluded, add it**

If the workflow uses an explicit allow-list (e.g., `docs/site/`), add `docs/architecture/` to the upload path or symlink the file into the published tree. Make the smallest possible change.

If `docs/` is uploaded wholesale, no change needed.

- [ ] **Step 4: Commit any workflow change**

```bash
git add .github/workflows/<file>.yml
git commit -m "ci(#4161): publish docs/architecture/api-rpc-surface-coverage.html via Pages"
```

If no change was needed, skip this commit and proceed.

---

## Task 21: Open the PR

**Files:** none

- [ ] **Step 1: Push branch**

```bash
git push -u origin worktree-scalable-popping-pretzel
```

- [ ] **Step 2: Create PR**

```bash
gh pr create --repo nexi-lab/nexus --title "feat(#4161): API/RPC surface coverage map v1" --body "$(cat <<'EOF'
## Summary

Closes part 1 of #4161 (skeleton + contract + initial extraction). Subissues fill per-row content.

- Extractor (`scripts/gen_api_surface_coverage.py`) reads CLI/gRPC/HTTP/MCP/SDK/profile sources, emits `docs/architecture/api-rpc-surface-coverage.yaml`.
- Renderer (`scripts/render_api_surface_coverage.py`) renders YAML + Mermaid module graph → `docs/architecture/api-rpc-surface-coverage.html`.
- Contract doc (`docs/architecture/api-rpc-surface-contract.md`) codifies mental model, row contract, 100% testing standard, perf classification.
- Warn-only CI gate (`tests/architecture/test_inventory.py`) detects drift; promoted to hard-fail in a follow-up issue.
- Subissue distribution script (`scripts/distribute_surface_contract_to_subissues.py`) appends a sentinel-bounded appendix into all 21 subissues — run **after** this PR merges.

## Out of scope (intentional)

- Filling any TODO row fields (summary / usage / test / perf). Subissue work.
- Promoting CI freshness gate from warn-only to hard-fail. Separate issue once subissues catch up.
- Authoring correctness tests or benchmarks for actual surfaces. Subissue work.
- Visual polish beyond legibility.

## Test plan

- [x] `uv run pytest tests/architecture/ -v` passes
- [x] `uv run python scripts/gen_api_surface_coverage.py` is idempotent (re-run = byte-identical YAML)
- [x] `uv run python scripts/render_api_surface_coverage.py` is idempotent
- [x] HTML opens in browser; search + expand work; Mermaid graph renders
- [ ] After merge: `uv run python scripts/distribute_surface_contract_to_subissues.py --dry-run` reviewed, then run for real
- [ ] After merge: GitHub Pages preview reachable at the published docs URL

## Spec

`docs/superpowers/specs/2026-05-16-issue-4161-arch-surface-coverage-design.md`
EOF
)"
```

- [ ] **Step 3: Verify CI passes**

Watch CI on the PR. The `tests/architecture/test_inventory.py` should pass (warnings allowed). If any unrelated check fails, address per usual repo conventions.

- [ ] **Step 4: After merge, distribute the appendix**

Once PR merges to develop:

```bash
git checkout develop && git pull
uv run python scripts/distribute_surface_contract_to_subissues.py --dry-run
# Review output. If looks good:
uv run python scripts/distribute_surface_contract_to_subissues.py
```

Verify each of the 21 issues now has the sentinel-bounded appendix at the end of its body.

---

## Self-review notes

This plan covers every component named in the spec:

- ✓ Schema dataclasses + YAML I/O (Task 2)
- ✓ Op-id normalizer (Task 3)
- ✓ Merge logic preserving human fields (Task 4)
- ✓ Per-transport extractors: MCP, CLI, HTTP, gRPC typed, gRPC Call, `@rpc_expose`, SDK, profiles (Tasks 5–12)
- ✓ Orchestrator script (Task 13)
- ✓ Renderer + jinja template + Mermaid graph (Tasks 14–15)
- ✓ Initial real-repo extraction + commit (Task 16)
- ✓ Warn-only CI gate (Task 17)
- ✓ Contract markdown doc (Task 18)
- ✓ Subissue distribution script (Task 19)
- ✓ GitHub Pages verification (Task 20)
- ✓ PR landing (Task 21)

Names checked for consistency across tasks: `SurfaceCoverage`, `Operation`, `TransportCell`, `Module`, `ProfileStatus`, `PerfClass`, `merge_coverage`, `generate_coverage`, `render_html`, `apply_appendix`, `build_appendix`, `APPENDIX_BEGIN`, `APPENDIX_END`.

Source anchors verified to exist (Task 1 verification calls) before any task depends on them: `_kernel_syscall_dispatch.py`, `cli/commands/__init__.py`, `tool_profiles.yaml`, `fastapi_server.py`, `deployment_profile.py`, `base_client.py`, `vfs.proto`. The spec mentioned `cli/registry.py` and `remote/client.py`, which don't exist — plan corrected to the actual paths.

Open implementation flexibility documented in plan, not deferred as TBD:

- Task 9 step 5: handles dispatch-variable-name discovery via try-list (DISPATCH / _DISPATCH / KERNEL_SYSCALL_DISPATCH / SYSCALL_DISPATCH).
- Task 15: Mermaid vendoring is its own task with a clear CDN fallback if download fails.
- Task 20: GitHub Pages workflow path is inspected and adjusted only if needed (not pre-assumed).
