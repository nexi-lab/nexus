# Design: Issue #4161 — Nexus API/RPC architecture map and 100% external surface coverage

- **Status:** Draft
- **Date:** 2026-05-16
- **Owner:** windoliver
- **Issue:** https://github.com/nexi-lab/nexus/issues/4161
- **Related epics:** #4119 (lite), #4120 (sandbox), #4121 (full), #4139 (shared coverage matrix)

## Goal

Ship v1 of the shared high-level architecture map for Nexus external APIs/RPCs as a single PR that:

1. Publishes a browsable HTML page enumerating every external surface (CLI, typed gRPC, generic gRPC `Call`, `@rpc_expose`, HTTP, MCP, SDK) grouped by owning module, with cross-transport parity visible at a glance.
2. Codifies the row contract, mental model, 100% testing standard, and performance classification in a separate, durable contract doc that the page references.
3. Distributes that contract into the 21 existing subissues (#4119/4120/4121/4139 + children #4122–#4138) as an appendix carrying acceptance-criteria deltas, slice-specific inventory guidance, test requirements, and gap rules.

This v1 deliberately does **not** fill per-surface usage examples, correctness tests, or performance classifications. Those are subissue work. v1 ships the scaffolding, the enumeration, the contract, and the distribution mechanism — so subissues have a single place to fill in.

## Non-goals

- Fill any TODO row fields (summary/usage_example/correctness_test/perf_class). Subissue work.
- Promote CI freshness gate from warn-only to hard fail. Separate issue once subissues catch up.
- Author tests for the actual surfaces. Subissue work.
- Visual polish beyond legibility (CSS theming, animations). Separate issue if desired.

## Mental model

Every external surface is traceable through this chain, embedded in the page and the contract doc:

```
profile → module/group → external API/RPC surface → how/when to use → correctness tests → performance classification → gap issue
```

A subissue is incomplete if it documents only command or method names. It must explain what module owns the surface, how users call it, what tests prove it works, and what remains missing.

## Architecture & data flow

```
source code         extractor        data file              build           output
─────────────       ─────────        ─────────              ─────           ──────
_kernel_syscall_   ┐
vfs.proto          │
@rpc_expose scan   │  →  scripts/   →  docs/architecture/  →  jinja2   →  docs/architecture/
cli/commands/**    │     gen_api_       api-rpc-surface-       render       api-rpc-surface-
tool_profiles.yaml │     surface-       coverage.yaml          (+mermaid    coverage.html
fastapi_server.py  │     coverage.py                            inline)
deployment_profile │
                   ┘
                          │
                          └─→ warn-only CI: tests/architecture/test_surface_inventory.py
                                            promoted to hard fail in v2
```

**Components:**

- `scripts/gen_api_surface_coverage.py` — extractor. AST + import walk + YAML/proto parse against the source anchors above. Idempotent. Preserves human-filled fields in YAML on regeneration.
- `docs/architecture/api-rpc-surface-coverage.yaml` — committed source of truth. One row per logical operation; cells per transport.
- `docs/architecture/api-rpc-surface-overrides.yaml` — human-curated op-id mappings, module overrides, and parity-gap acknowledgements.
- `scripts/render_api_surface_coverage.py` — jinja2 renderer. Reads YAML, emits HTML with embedded Mermaid graph + vanilla JS for search and fold/expand.
- `docs/architecture/api-rpc-surface-coverage.html` — published, browsable, committed.
- `docs/architecture/api-rpc-surface-contract.md` — mental model, row contract schema, 100% testing standard, perf classification taxonomy. Long-form reference linked from the HTML page.
- `tests/architecture/test_surface_inventory.py` — pytest, warn-only in v1. Three checks: freshness (re-extract diff), render determinism (re-render diff), schema validity.
- `scripts/distribute_surface_contract_to_subissues.py` — `gh issue edit` driver. Idempotent: marker sentinel detects existing appendix and replaces in-place.

**Two scripts (extract vs render) intentionally separate.** Subissues filling in TODOs only re-run render. Only adding/removing surfaces in code requires re-extract.

## Data schema

One row per **logical operation**. Each transport gets a cell, filled or `null`.

```yaml
# docs/architecture/api-rpc-surface-coverage.yaml
schema_version: 1
modules:
  - id: vfs
    name: "Virtual Filesystem"
    description: "Core read/write/stat/list path operations"
    depends_on: [kernel, rebac]
  # ... ~15-25 modules

operations:
  - id: fs.read                                # stable cross-transport key
    module: vfs
    summary: "Read bytes from a file path"     # extractor seeds from docstring; subissues refine
    transports:
      cli:        { name: "nexus fs read",         source: "src/nexus/cli/commands/fs.py:42" }
      grpc_typed: { name: "VFS.Read",               source: "proto/nexus/grpc/vfs/vfs.proto:88" }
      grpc_call:  { name: "fs.read",                source: "src/nexus/server/_kernel_syscall_dispatch.py:31" }
      http:       { name: "POST /api/v1/fs/read",   source: "src/nexus/server/fastapi_server.py:204" }
      mcp:        { name: "nexus_fs_read",          source: "src/nexus/config/tool_profiles.yaml:18" }
      sdk:        { name: "NexusClient.read",       source: "src/nexus/remote/client.py:97" }
    profiles:
      lite:    supported
      sandbox: supported
      full:    supported
    # filled by subissues (null in v1):
    usage_example: null      # CLI snippet + SDK snippet
    correctness_test: null   # path:line of pytest function
    perf_class: null         # hot | setup | control | not_perf_sensitive
    perf_link: null          # benchmark path or rationale text
    gap_issue: null          # github issue # if missing/stale/unsupported
    owning_issue: null       # subissue # responsible for this row
```

**Enums (validated by render):**

- `profiles.*`: `supported | unavailable | admin_only | deprecated | missing_needed`
- `perf_class`: `hot | setup | control | not_perf_sensitive`

**Parity gaps:** a missing transport cell is a potential gap. Extractor emits a top-level `parity_warnings:` block listing operations exposed via some transports but not others. Human triages by editing overrides YAML to either mark intentional (e.g., admin-only HTTP-less) or open a build issue and link in `gap_issue`.

## Extractor (`scripts/gen_api_surface_coverage.py`)

| Transport | Source | Technique |
|---|---|---|
| `cli` | `src/nexus/cli/commands/**/*.py` + `src/nexus/cli/registry.py` | AST: `@app.command()` / `@click.command()` decorators; lazy registry parse |
| `grpc_typed` | `proto/nexus/grpc/vfs/vfs.proto` | proto parse via `grpc_tools.protoc` plugin or regex on `rpc <Name>(...)` lines |
| `grpc_call` | `src/nexus/server/_kernel_syscall_dispatch.py` | AST: dispatch dict keys + handler function names |
| `grpc_expose` | `src/nexus/**/*.py` | AST: `@rpc_expose` decorator scan; capture method name + owning class |
| `http` | `src/nexus/server/fastapi_server.py` + sandbox allowlist | AST: `@router.{get,post,put,delete}` decorators; capture path + method |
| `mcp` | `src/nexus/config/tool_profiles.yaml` | YAML parse, enumerate tools per profile |
| `sdk` | `src/nexus/remote/client.py` + `src/nexus/remote/rpc_transport.py` | AST: public methods on `NexusClient` and wrappers |
| `profile_gates` | `src/nexus/contracts/deployment_profile.py` | AST: enum values + gate predicates |

**Op-id normalization** — canonical form `<module>.<verb>` (e.g., `fs.read`, `rebac.grant`). Per-transport names mapped via rule table:

- CLI `nexus fs read` → `fs.read`
- gRPC typed `VFS.Read` → `fs.read`
- MCP `nexus_fs_read` → `fs.read`
- HTTP `POST /api/v1/fs/read` → `fs.read`

Unmatched names land in `unmapped_surfaces:`; human assigns op-id in `api-rpc-surface-overrides.yaml`.

**Merge with existing YAML:** script reads committed YAML first, preserves human-filled fields (`summary` overrides, `usage_example`, `correctness_test`, `perf_*`, `gap_issue`, `owning_issue`), then merges newly-detected surfaces. Idempotent — same input yields byte-identical YAML.

**Module assignment:** initial heuristic from source path (`src/nexus/vfs/...` → `vfs`). Overrides in `api-rpc-surface-overrides.yaml`.

**Failure modes:**

- New surface in code, missing from YAML → added as row with `null` user-fields.
- Surface deleted from code, still in YAML → emitted in `stale_rows:` warning block; not deleted automatically (preserves history and links to old gap issues).
- Parity gap → `parity_warnings:` block, human triage required.

## HTML page (`docs/architecture/api-rpc-surface-coverage.html`)

Single self-contained file. Vendored Mermaid. Minimal vanilla JS — no framework. Hostable as static from GitHub Pages via existing docs workflow.

**Layout:**

```
┌────────────────────────────────────────────────────────────────┐
│  Nexus API/RPC Surface Map                                     │
├────────────────────────────────────────────────────────────────┤
│  [search]                                                      │
│                                                                │
│  §1  How to read this page                                     │
│      Each module groups external surfaces. Each surface is one │
│      logical operation exposed across CLI / RPC / HTTP / MCP / │
│      SDK. Subissues fill usage, test, perf, gap fields.        │
│      Full contract: api-rpc-surface-contract.md                │
│                                                                │
│  §2  Architecture                                              │
│      <Mermaid: modules + dependency arrows>                    │
│                                                                │
│  §3  Modules                                                   │
│       ▼ vfs — Virtual Filesystem                               │
│         • fs.read — read bytes from a path                     │
│           CLI:  nexus fs read          RPC:  VFS.Read          │
│           HTTP: POST /api/v1/fs/read   MCP:  nexus_fs_read     │
│           SDK:  NexusClient.read                               │
│           profiles: lite ✓  sandbox ✓  full ✓                  │
│           usage: TODO   test: TODO   perf: TODO                │
│           owner: #4123  gap: —                                 │
│         • fs.write — ...                                       │
│       ▶ rebac (click to expand)                                │
│       ▶ search                                                 │
│                                                                │
│  §4  Subissues filling this map                                │
│      #4119 lite  #4120 sandbox  #4121 full  #4139 matrix       │
└────────────────────────────────────────────────────────────────┘
```

**Behavior:**

- Search box filters surface rows by substring of op-id, transport name, or module.
- Modules collapsed by default except the first; click to expand.
- Per-transport rows show `name` + clickable source link (deep-link to GitHub via repo-URL convention).
- Profile cells: `✓` (supported), `✗` (unavailable), badge text for `admin_only` / `deprecated` / `missing_needed`.
- `TODO` for unfilled `usage` / `test` / `perf` cells. Subissues replace with content + links.
- `—` for transport cell = "not exposed via this transport". Visible inline; full parity-warning list lives in YAML, not HTML, to keep the page reader-friendly.

**Out of v1:**

- Filter dropdowns (profile/transport/status/module). Search-only in v1.
- Glyph legend with multiple status icons. Plain text labels.
- Standalone Row Contract / 100% Standard / Profile Gates sections in HTML. Those live in `api-rpc-surface-contract.md`, linked from §1.
- Parity-warnings section in HTML. Lives in YAML extractor output.

## Contract document (`docs/architecture/api-rpc-surface-contract.md`)

Long-form companion to the HTML page. Contents:

1. **Mental model** — the surface chain (`profile → module → surface → usage → test → perf → gap`) with rationale.
2. **Row contract** — 12 fields, types, enums, examples, what each subissue must fill.
3. **100% external API testing standard** — required coverage: positive flow, expected failure, auth/permission denied, profile-unavailable behavior, CLI/RPC parity, stable unsupported error shape, degraded behavior for optional dependencies.
4. **Performance classification** — `hot`, `setup`, `control`, `not_perf_sensitive` with definitions and what evidence each requires (benchmark link, latency guardrail, or explicit non-hot-path rationale).
5. **Workflow** — how subissues find their slice, fill rows, regenerate YAML, render HTML.
6. **Gap-issue rules** — when to open a build issue, required fields in the gap issue, how to link via `gap_issue` field.

## CI gate (`tests/architecture/test_surface_inventory.py`)

Pytest, warn-only in v1. Three checks:

1. **Freshness** — re-run extractor in temp dir, diff against committed YAML. New surfaces → warning per row, instruct to re-run extractor and commit.
2. **Render determinism** — re-run renderer, diff against committed HTML. Drift → warning.
3. **Schema validity** — YAML conforms to row contract (enums, required fields). Schema break → warning.

**Warning shape** (illustrative):

```
UserWarning: api-rpc-surface-coverage drift detected
  + new CLI command: nexus zone snapshot (src/nexus/cli/commands/zone.py:88)
  + new MCP tool: nexus_search_semantic (config/tool_profiles.yaml:42)
  Run: uv run python scripts/gen_api_surface_coverage.py
  Then commit the updated YAML and re-render the HTML.
  This is warn-only in v1. Will become a hard failure once subissues complete.
```

**Promotion to hard-fail** — single-line change: `warnings.warn(...)` → `pytest.fail(...)`. Done in a follow-up issue (likely owned by #4139), not this one. Rationale: during bootstrap, unrelated PRs that touch a new CLI command shouldn't be blocked.

## Subissue distribution

`scripts/distribute_surface_contract_to_subissues.py` — driver that calls `gh issue edit <n>` for each subissue with the standard appendix below. Idempotent: marker sentinel `<!-- BEGIN surface-contract-appendix:4161 -->...<!-- END surface-contract-appendix:4161 -->` lets re-runs replace in-place without duplication. Dry-run flag.

**Standard appendix:**

```markdown
<!-- BEGIN surface-contract-appendix:4161 -->
## Surface coverage contract (added by #4161)

This story slice contributes rows to the shared surface map:

- Map:      docs/architecture/api-rpc-surface-coverage.html
- Data:     docs/architecture/api-rpc-surface-coverage.yaml
- Contract: docs/architecture/api-rpc-surface-contract.md

### Your slice owns these surfaces (filter map by owner: #<this-issue>):

<auto-listed: operation ids assigned to this issue, kept in sync via the script>

### Acceptance-criteria delta

- [ ] Every owned row has `summary` and `usage_example` filled.
- [ ] Every owned row has `correctness_test` linking to a test file:line.
- [ ] Every owned row has `perf_class` set (`hot|setup|control|not_perf_sensitive`)
      and `perf_link` (benchmark path) — or rationale for `not_perf_sensitive`.
- [ ] Every owned row has `profiles.{lite,sandbox,full}` set.
- [ ] Any missing-needed surface has a build gap issue opened and linked via `gap_issue`.
- [ ] Re-run `scripts/gen_api_surface_coverage.py`; commit YAML; render HTML.
<!-- END surface-contract-appendix:4161 -->
```

**Slice assignment table** (initial extractor heuristic; overridable in `api-rpc-surface-overrides.yaml`):

| Issue | Slice |
|---|---|
| #4119 lite (epic) | profile gate inventory + lite-supported subset |
| #4122 | startup / mode contract + disabled-surface enumeration |
| #4123 | filesystem + metadata rows |
| #4124 | rebac + permissions rows |
| #4125 | scheduler + cache + status + admin rows |
| #4120 sandbox (epic) | sandbox profile gates + workspace-scoped surfaces |
| #4126 | `nexus up --profile sandbox` boot rows |
| #4127 | workspace fs + metadata rows |
| #4128 | rebac-boundary rows (workspace / hub / MCP) |
| #4129 | search rows (BM25S, sqlite-vec, semantic) |
| #4130 | federation rows |
| #4131 | MCP tool rows |
| #4121 full (epic) | full profile gates + hub-only surfaces |
| #4132 | hub startup + auth + remote-client rows |
| #4133 | core fs + streaming + batch rows |
| #4134 | full rebac + sharing + consent + dynamic-viewer rows |
| #4135 | search + parsing + semantic-indexing rows |
| #4136 | MCP + mounts + connectors + OAuth rows |
| #4137 | agents + workspaces + snapshots + versioning rows |
| #4138 | admin + audit + events + governance + federation + pay rows |
| #4139 | generator script ownership; promote CI warn → fail |

## File inventory (single PR)

```
docs/architecture/
  api-rpc-surface-coverage.html              (rendered, committed)
  api-rpc-surface-coverage.yaml              (extracted inventory, committed)
  api-rpc-surface-overrides.yaml             (human op-id mappings, module overrides)
  api-rpc-surface-contract.md                (mental model + row contract + standards)

scripts/
  gen_api_surface_coverage.py                (extractor)
  render_api_surface_coverage.py             (jinja2 → HTML)
  distribute_surface_contract_to_subissues.py (gh issue edit, idempotent)

scripts/templates/
  api-rpc-surface-coverage.html.j2           (jinja template)

tests/architecture/
  __init__.py
  test_surface_inventory.py                  (warn-only freshness + schema + render)
```

No source-code changes. Pure docs + tooling + tests.

## PR landing checklist

- [ ] `uv run python scripts/gen_api_surface_coverage.py` runs clean against current `develop`.
- [ ] `uv run python scripts/render_api_surface_coverage.py` runs clean and is idempotent (re-run = byte-identical HTML).
- [ ] `pytest tests/architecture/test_surface_inventory.py` passes (warnings allowed).
- [ ] HTML opens in a browser; Mermaid renders; search filters work; modules expand and collapse.
- [ ] All 21 subissues amended via `scripts/distribute_surface_contract_to_subissues.py` (run after PR merges).
- [ ] GitHub Pages preview reachable via existing docs workflow.
- [ ] PR description explicitly lists out-of-scope items (no TODO fills, no CI promotion, no test authoring).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Extractor misses a transport or surface | Manual review during PR; overrides YAML catches escapes; warn-only CI surfaces drift after merge |
| Mermaid graph too dense (>25 modules) | Group by tier (core / runtime / admin) at impl time based on extractor output |
| `gh issue edit` appendix marker collision | Unique sentinel `<!-- BEGIN surface-contract-appendix:4161 -->`; script removes-then-re-adds on idempotent re-runs |
| Op-id collisions across transports | Extractor emits `unmapped_surfaces:` requiring human override before YAML accepted |
| Subissue body edits unwelcome | `distribute_surface_contract_to_subissues.py --dry-run` for review; appendix is bounded by sentinels and removable |
| Existing GitHub Pages workflow doesn't auto-publish `docs/architecture/` | Verify path is in publish glob; if not, single-line workflow update is in scope |

## Decisions locked in

- **Renderer:** jinja2. Verify it's already in repo deps at impl time; if not, add to dev-deps.
- **Mermaid:** vendored as inline `<script>` in the generated HTML for offline + permanence; no CDN dependency.
- **Overrides YAML in v1:** ships empty (only `schema_version: 1` and empty maps). Populated as the first extraction surfaces real `unmapped_surfaces` / `parity_warnings` entries.
- **Subissue body edits:** run `scripts/distribute_surface_contract_to_subissues.py` only after PR merges to develop, not inside the PR itself. The script is part of the PR; its first execution is post-merge.

## Implementation-time verifications (not design ambiguities, just sanity checks)

- Confirm GitHub Pages publish glob covers `docs/architecture/*.html`; if not, single-line workflow update is in scope.
- Confirm jinja2 is in repo deps.
- Confirm pytest discovery picks up `tests/architecture/` automatically.
