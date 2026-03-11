# Test Plan

This plan is organized around the product users actually touch: embedded SDK, rich CLI, shared daemon/server, remote access, storage/search/memory features, and advanced federation/mount flows. The goal is not only correctness of isolated functions, but confidence that the same user workflows work across local, remote, and deployment-profile-specific paths.

## Test Goals

Every release should prove:

1. The embedded SDK can create, read, update, search, and close a local Nexus instance.
2. The CLI remains scriptable, human-usable, and consistent in JSON mode.
3. `nexusd` can boot a healthy shared node and expose the documented features for its profile.
4. The remote profile can reach that node through the documented HTTP and gRPC combination.
5. Feature bricks behave correctly when enabled, absent, or partially configured.
6. Policy, identity, search, memory, and workflow features do not regress when storage or transport changes.
7. High-risk packages with weaker direct coverage get explicit targeted tests before release.

## Test Layers

Use all of these layers together:

| Layer | Purpose | Existing anchors |
| --- | --- | --- |
| Unit | Fast feedback on contracts, pure logic, handlers, and adapters | `tests/unit/*` |
| Integration | Verify package boundaries, wiring, factories, exporters, and protocol conformance | `tests/integration/*` |
| E2E self-contained | Prove major features without external infrastructure | `tests/e2e/self_contained/*` |
| E2E server | Prove daemon/server/API/remote behaviors | `tests/e2e/server/*` |
| Infra-specific E2E | Validate Postgres, Redis, NATS, Docker, and other external dependencies | `tests/e2e/postgres`, `tests/e2e/redis`, `tests/e2e/nats`, `tests/e2e/docker` |
| Conformance | Keep APIs and behavioral contracts stable | `tests/conformance/*`, protocol tests |
| Benchmarks | Catch performance cliffs in hot paths | `tests/benchmarks/*` |
| Manual smoke | FUSE, live OAuth, and operator workflows that are hard to fully automate | `tests/manual/*` |

## Environment Matrix

Every release candidate should be exercised against the following matrix.

### Python and packaging

- Python 3.12
- Python 3.13
- Python 3.14
- editable install from source
- minimal install path from `requirements-minimal.txt`

### Deployment profiles

- `minimal`
- `embedded`
- `lite`
- `full`
- `remote` as a client profile against a live server

### Storage and persistence

- local CAS/path backend
- SQLite record store path
- Postgres-backed record store
- optional cloud/object-store connectors where supported by CI or isolated jobs

### Runtime and infrastructure

- HTTP server only
- HTTP plus gRPC remote path
- Redis/Dragonfly-backed cache behavior
- NATS/event export where applicable
- optional FUSE mount jobs on supported runners

### Feature toggles

- feature enabled
- feature disabled by profile
- feature disabled explicitly by `FeaturesConfig`
- missing dependency or optional extra not installed

## Use-Case Test Matrix

These are the release-blocking workflows, grouped by user-facing behavior rather than package names.

### A. Local SDK workflow

Prove:

- `nexus.sdk.connect()` works with a local profile and writable data dir.
- `sys_write`, `sys_read`, `sys_readdir`, `sys_stat`, `sys_unlink`, and `close` all work in sequence.
- path validation, metadata updates, versioning hooks, and lock behavior remain correct.
- local search and parsing features degrade cleanly when optional extras are unavailable.

Primary suites:

- `tests/unit/test_connect_quickstart.py`
- `tests/unit/core/*`
- `tests/unit/storage/*`
- `tests/integration/core/*`

Gaps to add:

- direct `tests/unit/sdk/*` coverage for `sdk.connect()` option resolution and failure modes
- explicit SDK parity tests for local versus remote profiles

### B. Terminal operator workflow

Prove:

- core commands work interactively and in pipelines
- `--json`, `--fields`, `--quiet`, and verbosity flags stay stable
- auto-JSON behavior for non-TTY stdout remains correct
- watch and interactive prompt flows behave correctly

Primary suites:

- `tests/unit/cli/*`
- `tests/e2e/self_contained/test_cli_commands_e2e.py`
- `tests/e2e/self_contained/test_cli_output_e2e.py`
- `tests/e2e/self_contained/test_cli_lifecycle.py`
- `tests/e2e/server/test_cli_commands_e2e.py`
- `tests/e2e/server/test_cli_domain_commands_e2e.py`

Gaps to add:

- snapshot tests for rich human output on key commands
- explicit tests for `status --watch` refresh behavior
- conflict-marker and import-smoke tests so broken command modules fail CI before runtime

### C. Shared daemon and remote client workflow

Prove:

- `nexusd` boots with expected config
- FastAPI health/auth/RPC surfaces come up
- gRPC transport works when enabled
- remote SDK and CLI can use the server without hidden local fallback

Primary suites:

- `tests/unit/daemon/test_main.py`
- `tests/unit/server/*`
- `tests/unit/grpc/*`
- `tests/unit/remote/*`
- `tests/e2e/server/test_api_v2_e2e.py`
- `tests/e2e/server/test_rpc_proxy_e2e.py`
- `tests/e2e/self_contained/test_proxy_integration.py`

Gaps to add:

- explicit end-to-end remote SDK smoke using the public `nexus.sdk.connect(profile="remote")`
- stronger integration tests under `tests/integration/remote/*`

### D. Search and retrieval workflow

Prove:

- glob/grep semantics remain stable
- parsed search works for supported document types
- hybrid or semantic retrieval behaves correctly when extras are installed
- contextual chunking, ranking, and query expansion do not regress silently

Primary suites:

- `tests/unit/bricks/search/*`
- `tests/unit/bricks/parsers/*`
- `tests/e2e/server/test_contextual_chunking_e2e.py`
- `tests/e2e/server/test_query_expansion_e2e.py`
- `tests/e2e/server/test_trigram_grep_e2e.py`
- `tests/e2e/self_contained/test_trigram_search_integration.py`

Gaps to add:

- direct package tests for top-level namespace packages that currently rely on brick tests indirectly
- fixture-based regression tests for ranking quality and parser fallback behavior

### E. Identity, auth, and authorization workflow

Prove:

- auth providers, API keys, OAuth, and token rotation behave correctly
- ReBAC policy checks hold at the CLI, server, and storage levels
- access manifests correctly scope visible and invokable tools
- delegation and identity credentials preserve expected trust boundaries
- permission failures are explicit and stable

Primary suites:

- `tests/unit/server/test_auth_*`
- `tests/unit/bricks/access_manifest/*`
- `tests/unit/bricks/delegation/*`
- `tests/unit/bricks/governance/*`
- `tests/e2e/server/test_auth_security_e2e.py`
- `tests/e2e/server/test_identity_e2e.py`
- `tests/e2e/server/test_delegation_full_e2e.py`
- `tests/e2e/server/test_directory_grants_e2e.py`
- `tests/e2e/server/test_wildcard_public_access_e2e.py`

Gaps to add:

- broader package-level tests for `bricks.rebac` internals
- lifecycle coverage for `bricks.access_manifest`, not only evaluator behavior
- golden-path user stories that combine identity, delegation, and ReBAC in one flow

### F. Memory, workspace, and workflow automation

Prove:

- memory append-only, paging, versioning, and evolution flows work
- workspace registration and context branching stay consistent
- workflow triggers and scheduler behavior survive restarts
- validation and sandbox execution integrate with those flows
- agent-tool wrappers from `nexus.tools` remain aligned with underlying file, memory, and sandbox behavior

Primary suites:

- `tests/unit/bricks/memory/*`
- `tests/unit/services/*`
- `tests/unit/scheduler/*`
- `tests/e2e/server/test_memory_*`
- `tests/e2e/self_contained/test_context_branch_lifecycle.py`
- `tests/e2e/self_contained/test_scheduler_integration.py`
- `tests/e2e/self_contained/test_validation_pipeline.py`
- `tests/e2e/server/test_validation_e2e.py`

Gaps to add:

- direct tests for `tasks` package runners and abandonment/requeue behavior
- stronger package-level tests for `system_services` instead of only indirect e2e validation
- direct checked-in tests for `nexus.tools`

### G. Upload, sync, versioning, and recovery

Prove:

- resumable upload works
- delta sync and edge sync stay correct under conflict and retry conditions
- snapshots and version history remain recoverable
- audit and operation logs are complete enough for rollback/debugging

Primary suites:

- `tests/unit/cli/test_upload_cli.py`
- `tests/e2e/server/test_tus_upload_e2e.py`
- `tests/e2e/server/test_delta_sync_e2e.py`
- `tests/e2e/server/test_edge_sync_e2e.py`
- `tests/e2e/server/test_conflict_api_e2e.py`
- `tests/e2e/server/test_operations_e2e.py`
- `tests/e2e/server/test_write_back_e2e.py`

Gaps to add:

- more direct unit tests for `proxy` replay and offline queue edge cases
- recovery tests that combine uploads, version history, and operation replay

### H. Federation, networking, and mount workflows

Prove:

- zone join/export/import/deprovision flows behave correctly
- WireGuard and TLS bootstrap generate valid config/artifacts
- FUSE mounting preserves expected read/write/list behavior

Primary suites:

- `tests/unit/raft/*`
- `tests/unit/security/test_tls.py`
- `tests/unit/fuse/*`
- `tests/e2e/docker/test_federation_e2e.py`
- `tests/e2e/docker/test_raft_cluster_smoke.py`
- `tests/e2e/server/test_zone_*`
- `tests/e2e/server/test_namespace_fuse_e2e.py`
- `tests/e2e/server/test_fuse_events_e2e.py`

Gaps to add:

- direct tests for `network` package and CLI-generated WireGuard artifacts
- failure-injection tests for real federation cluster partitions and cert rotation

## Package-by-Package Coverage Plan

This table focuses on the top-level packages that currently ship source.

| Package | What must be proven | Current coverage shape | Required additions |
| --- | --- | --- | --- |
| `sdk` | config dispatch, local/remote parity, public import stability | indirect via core/remote/e2e | add direct `tests/unit/sdk/*` |
| `cli` | command registration, JSON contract, interactive ergonomics, pipe-safe output | strong unit and e2e coverage | add richer output snapshots and watch-mode tests |
| `daemon` | boot, pid/ready files, config/env handling, profile guardrails | basic unit coverage plus server e2e | add more failure-path tests |
| `server` | auth, RPC dispatch, lifespan, health, websockets, observability | strong unit and e2e coverage | keep expanding profile-specific smoke |
| `grpc` | typed remote operations, streaming, startup gating | modest unit coverage | add more auth/error-path tests |
| `remote` | remote FS proxy correctness, retries, timeouts, service dispatch | modest unit plus e2e proxy tests | add integration tests around live server |
| `plugins` | discovery, lazy load, command wiring, scaffold output | little direct source coverage | add package-level unit tests |
| `core` | syscall semantics, routing, locking, metadata/object-store boundaries | strong unit/integration | add more concurrency and fast-path regression tests |
| `contracts` | protocol stability, exceptions, types, capability flags | good unit/protocol coverage | add end-to-end contract parity checks |
| `storage` | metastore/record store correctness, persistence, history, models | strong unit plus e2e domain tests | add deeper workflow tests for weaker domains |
| `backends` | local/cloud/connector behavior, wrappers, uploads, CDC | strong unit, connector tests, targeted e2e | increase live or near-live provider coverage |
| `cache` | cache coherency and invalidation | solid unit plus targeted e2e | add more external-cache driver jobs |
| `factory` | correct profile wiring and graceful degraded boot | moderate unit/integration | add profile matrix smoke |
| `fuse` | mount semantics and host compatibility | solid unit plus targeted integration/e2e | keep manual smoke for OS-specific edges |
| `raft` | zone state, metadata replication, federation lifecycle | some unit and strong scenario tests | add cluster-failure injection |
| `network` | WireGuard config and peer lifecycle correctness | weak direct coverage | add dedicated unit and CLI tests |
| `security` | TLS config, trust bootstrap, join tokens | narrow direct coverage | add more server/federation integration cases |
| `proxy` | replay, conflict detection, offline queue, transport resilience | partial unit plus e2e | add failure-path and recovery tests |
| `system_services` | lifecycle, scheduler, sync, workspace and namespace internals | mostly indirect coverage | add direct package-focused tests |
| `tasks` | runner healthbeats, retries, requeue, abandonment | mostly indirect coverage | add direct unit and integration tests |
| `tools` | tool wrappers, prompt bundles, auth/context propagation | weak direct coverage | add package-level tests for `nexus.tools` |
| `validation` | runner and parser interoperability | mostly e2e-oriented | add direct unit tests |
| `utils` | edit engine correctness and edge behavior | small targeted unit coverage | keep fuzz/property tests on edit engine |
| `lib` | helpers stay stable where used by server/core/services | scattered indirect coverage | add focused tests for higher-risk helpers |
| `migrations` | upgrade path correctness and rollback safety | sparse direct coverage | add migration smoke and downgrade checks |

## Feature-Brick Coverage Plan

Bricks are where much of the user-visible product surface lives. They should always be tested at three levels:

1. unit tests inside `tests/unit/bricks/<brick>`
2. at least one server or self-contained scenario test
3. one negative-path test for missing config, disabled feature, or dependency failure

Priority bricks and expected coverage:

| Brick | Current evidence | Add next |
| --- | --- | --- |
| `auth` | strong server auth tests and e2e auth flows | more disabled-feature/profile-gating tests |
| `rebac` | broad cross-layer coverage, but spread across packages | more direct brick-focused tests |
| `search` | strong brick unit tests and e2e retrieval flows | ranking and parser-regression fixtures |
| `parsers` | strong brick unit tests | more cross-provider fallback tests |
| `memory` | unit plus many e2e flows | add restart/recovery tests |
| `mcp` | strong brick unit tests and self-contained integration | add remote/daemon parity smoke |
| `llm` | solid unit tests | add end-to-end provider contract smoke |
| `sandbox` | many e2e flows | add direct unit tests around routing and auth edges |
| `workflows` | mostly broader service/e2e validation | add brick-focused unit tests |
| `pay` | targeted unit and e2e coverage | add failure and fraud-policy tests |
| `governance` | strong unit coverage and server e2e | add larger scenario chains with reputation/pay |
| `identity` | strong scenario tests | add more direct unit tests around credential lifecycle |
| `delegation` | some unit plus e2e | add more revocation and expiry cases |
| `ipc` and `a2a` | good unit and e2e coverage | add cross-zone and failure-path cases |
| `snapshot` and `versioning` | some targeted tests | add combined recovery and rollback scenarios |
| `upload` and `share_link` | mostly e2e coverage | add more direct unit coverage |

## CLI/TUI Coverage Strategy

There is no first-party full-screen TUI today, so test the shipped terminal experience explicitly.

### Rich CLI checks

- human output remains readable on a TTY
- JSON output remains stable off-TTY
- prompts, confirms, and watch views behave correctly
- command help stays coherent and loads without import failures

### Future TUI path

If the team builds a TUI later, require:

- SDK-backed integration tests for screen actions
- snapshot tests for critical views
- parity tests against CLI JSON responses
- remote and local profile coverage

## Release Gates

A release should not ship unless all of these are true:

1. Unit, integration, and both major e2e suites are green on the supported Python matrix.
2. Quickstart SDK and CLI smoke tests pass from a clean install.
3. Remote client smoke passes against a live `nexusd` with gRPC enabled.
4. At least one Postgres-backed job and one cache/event-infra job pass.
5. No unresolved merge markers or import-time syntax failures remain in checked-in Python sources.
6. Feature-gated packages fail cleanly when disabled or optional deps are absent.
7. High-risk weak-coverage packages have either new tests or an explicit release waiver.

## Highest-Priority Gaps

These are the best next investments based on the current tree:

1. Add direct package tests for `sdk`, `plugins`, `tasks`, `validation`, and `system_services`.
2. Add dedicated `network` tests; this is the clearest top-level package gap.
3. Add direct checked-in coverage for `nexus.tools` and lifecycle coverage for `bricks.access_manifest`.
4. Add live-server integration coverage for the public remote SDK path.
5. Add richer CLI UX regression tests around watch mode, prompts, and human output.
6. Add failure-injection coverage for federation, proxy replay, and background task recovery.
