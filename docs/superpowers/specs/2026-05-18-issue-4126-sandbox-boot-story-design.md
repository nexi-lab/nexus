# Design — Issue #4126: `nexus up --profile sandbox` boot story and smoke tests

- **Issue:** [#4126](https://github.com/nexi-lab/nexus/issues/4126) (parent epic [#4120](https://github.com/nexi-lab/nexus/issues/4120))
- **Date:** 2026-05-18
- **Branch:** `issue-4126-sandbox-boot-story`

## Problem

An agent runtime operator wants one command to start a lightweight Nexus inside a
sandbox and to know what it starts locally. The boot story for the `sandbox`
deployment profile is undocumented in the user guide, has no end-to-end smoke
test proving it boots with no external services, and its surfaces are not
classified for the shared coverage matrix.

## Current state (verified)

Most product surface already exists:

- `src/nexus/cli/commands/stack.py` — `nexus up --profile sandbox` with
  `--workspace`, `--hub-url`, `--hub-token`; validates sandbox-only flags require
  `--profile sandbox`; `--hub-url` requires `--hub-token`. Shells out to `nexusd`
  (or `python -m nexus.daemon.main`).
- `src/nexus/daemon/main.py` — `nexusd --profile sandbox` with same flags; same
  validation; `SandboxBootstrapper` when `workspace` set. Writes readiness file
  `~/.nexus/nexusd.ready` (fixed path under `$HOME`). `nexusd` rejects
  `profile=remote`.
- `src/nexus/contracts/deployment_profile.py` — `DeploymentProfile.SANDBOX`;
  `_SANDBOX_BRICKS` = `_LITE_BRICKS` + search + mcp + parsers; sandbox drivers are
  local + connectors. No Postgres/Redis/Zoekt.
- HTTP `/health` (public) and `/health/detailed` (admin) —
  `src/nexus/server/api/core/health.py`.
- HTTP `/api/v2/features` — `src/nexus/server/api/core/features.py` returns
  `profile`, `enabled_bricks`, `disabled_bricks`, `version`.
- gRPC `Ping` — `proto/nexus/grpc/vfs/vfs.proto`.
- `nexus status` (`status.py`, Docker/HTTP oriented), `nexus env` (`env_cmd.py`).
- `tests/unit/cli/test_stack_sandbox.py` — already comprehensive CLI flag
  validation (happy path, fallback, env vars, all four usage-error cases).
- `docs/deployment/sandbox-profile.md` — exists, describes runtime, bricks,
  federation, pip/Docker examples.
- `docs/guides/user-guide.md` — exists; "Pick The Right Mode" lists
  full/lite/cloud/remote but **omits `sandbox`**.

The shared profile coverage matrix is a separate deliverable owned by
[#4139](https://github.com/nexi-lab/nexus/issues/4139); #4126 contributes its
rows, it does not build the matrix.

## Deliverables

### 1. User guide section
`docs/guides/user-guide.md`, new "Sandbox profile (per-agent runtime)"
subsection under "Pick The Right Mode":
- User goal + why the sandbox profile supports it.
- CLI example: `nexus up --profile sandbox --workspace ~/app`; direct
  `nexusd --profile sandbox --workspace ~/app`.
- RPC parity examples: `curl /health`, `curl /api/v2/features`,
  `grpcurl ... Ping`.
- Expected success / denial (`--workspace` without `--profile sandbox` → usage
  error; `--hub-url` without `--hub-token` → usage error) / unavailable
  (sandbox-provisioning brick disabled) behavior.
- Correctness assertion the user can run: `/api/v2/features` shows
  `profile=sandbox` and `disabled_bricks` includes
  `pay`/`llm`/`workflows`/`sandbox`/...; boots with no Postgres/Redis/Zoekt.
- Observed warm boot, cold boot, RSS numbers, with explicit "setup path /
  control plane — not performance-sensitive" note.
- Cross-link to `docs/deployment/sandbox-profile.md`.

### 2. Profile-vs-brick clarification
Short "Not to be confused with" callout in both `docs/guides/user-guide.md` and
`docs/deployment/sandbox-profile.md`: the `sandbox` *deployment profile* (how
Nexus runs) vs `BRICK_SANDBOX` *sandbox-provisioning brick* (code-execution
feature, disabled in this profile). Orthogonal concepts.

### 3. Story coverage table
A small table in the guide section classifying this story's surfaces — CLI
(`up`, `--profile`, `--workspace`, `--hub-url`, `--hub-token`), HTTP `/health`,
HTTP `/api/v2/features`, gRPC `Ping`, `nexus status`, `nexus env` — with
columns: surface type, sandbox status, example link, test link, benchmark
classification. Owning story #4126. Note that #4139 aggregates into the shared
matrix.

### 4. Smoke test (real subprocess boot)
New `tests/integration/sandbox/test_sandbox_boot_smoke.py`, marked
slow/integration:
- Spawn `nexusd --profile sandbox --workspace <tmp>` as a subprocess with an
  isolated `HOME` (so the `~/.nexus/nexusd.ready` path is per-test and
  parallel-safe) and isolated data dir.
- Poll the readiness file until ready or timeout.
- Assert the process boots with **no Postgres/Redis/Zoekt** running (none
  started by the harness; no connection attempts/errors in output).
- HTTP `/health` → 200; `/api/v2/features` → `profile=sandbox` and expected
  disabled bricks; gRPC `Ping` → not reachable under sandbox in this configuration (connection refused in fresh empirical test). Behavior is contested — issue #4148 (open, parent #4126) reports it returns UNAUTHENTICATED. Recorded as a tracked gap (#4148), NOT asserted as intentional/by-design.
- Capture warm boot time, cold boot time, RSS via `psutil`; assert loose upper
  bounds (guard gross regression only — no statistical baselines, no CI gate).
- Cover positive flow, denied flow (parity with CLI usage errors), profile
  gating, CLI/RPC parity.

### 5. CLI tests
`tests/unit/cli/test_stack_sandbox.py` is already comprehensive. Add only a
CLI/RPC parity assertion if a gap exists. No rewrite.

### 6. Missing-surface gate — readiness/discovery gap CLOSED in this PR (revised 2026-05-18)
Verdict: all core surfaces for the boot story exist. The substantive gap was
not merely a missing readiness *probe* — it was that `nexus up --profile
sandbox` ran `nexusd` as a blocking subprocess and persisted **no runtime
state**, so a sandbox started on a non-default host/port could not be
discovered by the follow-up `nexus env` / `nexus run` / `nexus status`
workflow (those hard-require a project config + `<data_dir>/.state.json`).
That readiness/discovery gap is **closed in this PR by implementing
[#4144](https://github.com/nexi-lab/nexus/issues/4144)** (state persistence),
not by a standalone probe alone.

**#4144 — sandbox `up` persists connection state.**
`nexus up --profile sandbox` now accepts and passes `--host`/`--port`/
`--data-dir` through to `nexusd --profile sandbox` (existing
`--workspace`/`--hub-url`/`--hub-token` validation preserved). It derives the
gRPC port the way `nexusd` does (`http_port + 2` unless overridden by env
`NEXUS_GRPC_PORT`; an explicit `--port` wins over the env override — mirrors
`src/nexus/daemon/main.py`), then **before** the blocking daemon runs it:

- writes `<data_dir>/.state.json` via `save_runtime_state` recording
  `profile=sandbox`, `workspace`, resolved `ports.http`/`ports.grpc`, and the
  bind `grpc_host`;
- writes a *minimal* `nexus.yaml` into the discovery location **only if one is
  absent** (an existing project config is never clobbered).

Discovery-anchor rationale (smallest blast radius): `nexus env`/`run`
hard-require `load_project_config()` (raises without `nexus.yaml`). Making
those three consumers fall back to optional config would touch three call
sites and risk regressing the Docker `up` resolution path. Writing a minimal
`nexus.yaml` localizes the change entirely to the sandbox-only branch of
`up`; the Docker `up` flow and existing env/status code are untouched.
`resolve_connection_env` gains `NEXUS_PROFILE`/`NEXUS_WORKSPACE` and a
state-recorded `grpc_host`, all emitted **only when present in state** — the
Docker path does not set those keys, so its env output is unchanged.
Secrets: `--hub-url` MAY be recorded; `--hub-token` is **never** written to
persistent state (proven by a grep assertion in the unit tests).

Coverage: `tests/unit/cli/test_stack_sandbox.py::TestSandboxStatePersistence`
(flag pass-through, state shape, gRPC-port derivation incl. env override and
explicit-port precedence, hub-token-not-persisted grep, no-clobber, end-to-end
`up`→`nexus env` discovery) and an additive integration test in
`tests/integration/test_sandbox_boot_smoke.py`
(`test_sandbox_up_state_is_consumed_by_status`) asserting `nexus env`/`nexus
status` consume the persisted state against the real booted sandbox daemon.

**`nexus ready` remains a complementary readiness probe** (not the gap
closure). `nexus ready [--timeout SECONDS] [--readiness-file PATH] [--json]`
waits for `~/.nexus/nexusd.ready`, parses `host:port`, polls `GET /health`
and `GET /api/v2/features`, prints profile / endpoint / health /
enabled-bricks, and uses sysexits codes (`0` ready; `TEMPFAIL` 75 on
timeout; `DATA_ERROR` 65 on a malformed readiness file). A standalone command
(not `nexus status --profile sandbox`) because `status`'s `--profile` already
means *compose profiles*. Covered by `tests/unit/cli/test_ready_cmd.py` and
the `sandbox_daemon` integration test. Benchmark class: control plane / setup
path — not performance-sensitive.

No build issue is filed (gap closed in-PR via #4144). The user guide's
missing-surface gate verdict and coverage table are updated accordingly.

## Out of scope
- Building the #4139 shared matrix generator.
- Benchmark CI regression gates / statistical baselines.
- Rewriting existing CLI tests.
- Workspace / ReBAC / search / federation / MCP stories (#4127–#4131).

## Testing strategy
- TDD for the smoke test.
- Real subprocess boot; isolated `HOME` for parallel safety and to scope the
  fixed `~/.nexus/nexusd.ready` path.
- Boot/RSS measured but classified non-performance-sensitive (loose bounds
  only).

## Risks
- Fixed readiness path under `$HOME`: mitigated by per-test `HOME` env override.
- Subprocess boot may be slow / flaky in CI: mitigated by slow/integration
  marker and generous timeout with readiness polling.
- gRPC `Ping` under sandbox: empirically connection-refused here, but open issue #4148 contests this (claims UNAUTHENTICATED). Status is unresolved and tracked by #4148; do not document it as "intentional/by-design".
