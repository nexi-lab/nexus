# Nexus API/RPC Surface Coverage Contract

This document is the source of truth for how surfaces are inventoried, classified,
tested, and benchmarked. The interactive map `api-rpc-surface-coverage.html` is the
rendered view of the data this contract governs — it is generated from
`api-rpc-surface-coverage.yaml` by `scripts/render_api_surface_coverage.py`.

Two ways to read it:

- **Online (published)** — the `Documentation` workflow re-renders on every push
  to `develop` and publishes to GitHub Pages at
  <https://nexi-lab.github.io/nexus/surface-coverage/api-rpc-surface-coverage.html>.
- **Offline (local)** — run `uv run python scripts/render_api_surface_coverage.py`
  to regenerate `api-rpc-surface-coverage.html` in this directory and open it
  in a browser. The local HTML is **not committed** (`.gitignore`d).

## Mental model

Every external surface is traceable through this chain:

~~~
profile → module/group → external API/RPC surface → how/when to use → correctness tests → performance classification → gap issue
~~~

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
| `profiles` | extractor (default supported) → subissue overrides | Status per profile: `supported \| unavailable \| admin_only \| deprecated \| missing_needed`. |
| `usage_example` | subissue | CLI snippet + SDK snippet showing real invocation. |
| `correctness_test` | subissue | `path:line` of a pytest function exercising the surface. |
| `perf_class` | subissue | `hot \| setup \| control \| not_perf_sensitive`. |
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

1. Open the map — either the published view at
   <https://nexi-lab.github.io/nexus/surface-coverage/api-rpc-surface-coverage.html>,
   or render locally with `uv run python scripts/render_api_surface_coverage.py`
   then open `api-rpc-surface-coverage.html`. (The local HTML is a generated
   artifact and is not committed; see the note above.)
   Filter / search for rows where `owner: #<your-issue>`.
2. For each row, fill `summary`, `usage_example`, `correctness_test`, `perf_class`, `perf_link`, `profiles` in `api-rpc-surface-coverage.yaml`.
3. If the surface is missing-needed (no implementation yet), open a build issue and set `gap_issue`.
4. Re-run `uv run python scripts/gen_api_surface_coverage.py` (merge with your edits) then `uv run python scripts/render_api_surface_coverage.py`.
5. Commit the updated **YAML** (`api-rpc-surface-coverage.yaml`). The HTML is
   regenerated from it on demand and is `.gitignore`d — do not commit it.

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
- gRPC `Call` (syscalls): `src/nexus/server/_kernel_syscall_dispatch.py` (`KERNEL_SYSCALL_NAMES` frozenset)
- `@rpc_expose`: scan of `src/nexus/**/*.py`
- HTTP: `src/nexus/server/fastapi_server.py`
- MCP: `src/nexus/config/tool_profiles.yaml`
- SDK: `src/nexus/remote/base_client.py` (`BaseRemoteNexusFS`)
- Profiles: `src/nexus/contracts/deployment_profile.py` (`DeploymentProfile`)
