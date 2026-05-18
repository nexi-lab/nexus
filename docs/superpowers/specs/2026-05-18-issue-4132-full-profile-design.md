# Issue #4132 — Shared hub startup, auth, remote client, and profile contract

**Issue**: [nexi-lab/nexus#4132](https://github.com/nexi-lab/nexus/issues/4132)
**Epic**: [#4121 — full profile CLI/RPC user story, tests, and benchmarks](https://github.com/nexi-lab/nexus/issues/4121)
**Story group**: Shared hub startup, auth, remote clients, and profile contract

## Problem

A team operator wants to start a full Nexus hub and connect CLI/SDK
clients remotely, with clear auth and gRPC requirements. Today the
relevant knowledge is split across `CLI.md`, `docs/paths/daemon-and-remote.md`,
and source (`deployment_profile.py`, `daemon/main.py`), with no single
coherent workflow and at least one stale path doc. There is no
`docs/deployment/full-profile.md` (the sibling `sandbox` profile has
one); the FULL deployment-profile contract is not asserted by tests; and
no user-runnable command prints the resolved profile contract.

## Non-goals

- The `cloud` profile, federation, or multi-tenant behavior (FULL
  deliberately excludes `federation` — that is `cloud`).
- Implementing any missing CLI/RPC surface. Gaps are enumerated and
  filed as linked build issues; this story documents and tests the
  existing surface and is blocked from closing until required gaps are
  tracked.
- Sibling feature surfaces (#4133–#4138): filesystem, ReBAC, search,
  MCP, agents, admin. This story covers startup/auth/remote/contract
  only.
- A new shared test-harness package. We add one reusable, profile-
  agnostic boot fixture, not epic-wide scaffolding.
- Windows. CI covers Linux + macOS only (matches sibling #3778).

## Decisions (from brainstorming)

| # | Question | Decision |
|---|---|---|
| Q1 | Doc shape | **Hybrid**: standalone `docs/deployment/full-profile.md` (profile contract reference) + a "start a shared hub & connect remotely" narrative section in `docs/guides/user-guide.md` that links to it |
| Q2 | Scope width | #4132 only + light matrix wiring. Reuse #4161 `api-rpc-surface-coverage.yaml`/`gaps.yaml` as-is; add only `full` startup/auth/status rows |
| Q3 | Test depth | Always-on unit/contract + parity tests (no Docker); one real-Docker-boot E2E + benchmarks gated behind `NEXUS_E2E=1` (`@pytest.mark.integration`). Matches sibling #3778 |
| Q4 | Missing-surface gate | Enumerate every gap with a full build-issue template in this spec; file via `gh issue create` and link to #4132/#4121 **only after user approval** |
| Q5 | Benchmark posture | Boot/RSS/connect = setup path (recorded as guidance, not CI gates); health/features/Ping = control plane (latency asserted with generous bounds); no steady-state data-plane hot path in this story |
| Q6 | Boot fixture reuse | Boot fixture written profile-agnostic so siblings #4133–#4138 can reuse it; no speculative abstraction beyond that |

## Source anchors

- `CLI.md`
- `src/nexus/cli/commands/init_cmd.py`
- `src/nexus/cli/commands/stack.py`
- `src/nexus/daemon/main.py`
- `src/nexus/contracts/deployment_profile.py`
- `src/nexus/cli/compose.py`, `docker-compose.yml`, `Dockerfile`
- `docs/paths/daemon-and-remote.md`
- Precedent: `docs/deployment/sandbox-profile.md`,
  `docs/superpowers/specs/2026-04-17-3778-sandbox-profile-design.md`
- Matrix: `docs/architecture/api-rpc-surface-coverage.yaml`,
  `docs/architecture/api-rpc-surface-gaps.yaml`

## Ground-truth facts (verified against source)

- `DeploymentProfile.FULL.default_bricks()` = `_LITE_BRICKS` ∪
  {search, pay, llm, skills, sandbox, workflows, discovery, mcp, memory,
  task_manager, observability, uploads, resiliency, access_manifest,
  catalog, delegation, identity, share_link, versioning, workspace,
  portability, parsers, snapshot}. **Excludes `federation`** (that is
  `cloud = full ∪ {federation}`).
- `DeploymentProfile.FULL.default_drivers()` = sandbox drivers ∪
  {s3, gcs, gdrive, gmail, slack, x, hn, remote}.
- `nexusd --profile remote` exits `ExitCode.CONFIG_ERROR` with
  "A daemon cannot be a thin client of another daemon." — documented
  denial behavior.
- Daemon auth: `--auth-type database` → `DatabaseAPIKeyAuth` over
  `--database-url`/`POSTGRES_URL`; else static via
  `--api-key`/`NEXUS_API_KEY`/`NEXUS_API_KEY_FILE`.
- **Three distinct "profile" namespaces** (the core source of reader
  confusion the guide must disambiguate):
  1. *Docker Compose profiles* (`compose.py` `VALID_PROFILES`:
     `core`, `cache`, …) — service selectors; presets map to
     `compose_profiles` tuples (shared/demo → `("core","cache")`).
  2. *CLI connection profiles* (`config.py`, `nexus profile use/add`) —
     kubectl-style URL/key/zone entries in `~/.nexus/config.yaml`.
  3. *Deployment profile* (`DeploymentProfile`, `nexusd --profile full`,
     env `NEXUS_PROFILE`) — the brick/driver capability tier.
- Verified mechanism: the `nexus up` Docker stack runs the FULL
  deployment profile because `docker-compose.yml:23` sets
  `NEXUS_PROFILE=full` (and `Dockerfile:269` defaults the same). The
  preset does **not** set the deployment profile directly — the compose
  env var does. No preset is literally named `full`.
- Remote SDK: `connect(config={"profile":"remote","url":...,
  "api_key":...})` requires gRPC reachable (`NEXUS_GRPC_PORT`); the HTTP
  URL alone is insufficient.
- Stale doc: `docs/paths/daemon-and-remote.md` uses `--profile minimal`
  (not a member of `DeploymentProfile`) and omits `nexus up`.

## Deliverables

| Artifact | Path | Purpose |
|---|---|---|
| Profile guide | `docs/deployment/full-profile.md` (new) | FULL contract: brick/driver set, HTTP/gRPC surface, auth modes, denial behavior, benchmark guidance |
| User narrative | `docs/guides/user-guide.md` (new section) | init→up→env→status→remote-connect story; links to profile guide; CLI + SDK examples; success/denied/unavailable behavior |
| Stale-doc fix | `docs/paths/daemon-and-remote.md` | `--profile minimal` → `full`; add preset/`nexus up` path; keep raw-`nexusd` path; state gRPC requirement explicitly |
| Contract tests | `tests/unit/core/test_full_profile.py` (new) | FULL brick/driver set; superset over LITE; excludes federation; `nexusd --profile remote` rejected |
| CLI/RPC parity tests | `tests/unit/cli/`, `tests/unit/daemon/` | `nexus env/status --json` shape; daemon `--profile full` banner; static vs database auth resolution |
| Gated E2E + bench | `tests/integration/test_full_profile_boot.py` (new) | Real Docker boot; health/features/Ping; remote SDK connect; boot-time/RSS capture. `@pytest.mark.integration`, skip unless `NEXUS_E2E=1` |
| Matrix rows | `docs/architecture/api-rpc-surface-coverage.yaml` | `full` status for startup/auth/env/status operations |
| Gap backlog | `docs/architecture/api-rpc-surface-gaps.yaml` + §"Missing-surface gate" | Enumerate gaps; GitHub issues filed only on user approval |

## Profile-contract assertions (verifiable correctness checks)

The guide states, and tests prove:

1. `DeploymentProfile.FULL.default_bricks()` ⊇ `LITE.default_bricks()`,
   includes the 18 feature bricks listed above, and **excludes**
   `federation`.
2. `DeploymentProfile.FULL.default_drivers()` includes
   {s3, gcs, gdrive, remote} and the sandbox connector set.
3. `nexusd --profile remote` exits non-zero with the documented message.
4. Static auth: request without API key → 401; with key → 200.
   Database auth: key validated via `DatabaseAPIKeyAuth`.
5. Remote SDK with no reachable gRPC → explicit failure; with
   `NEXUS_GRPC_PORT` set and reachable → success.
6. `nexus env --json` emits `NEXUS_URL`, `NEXUS_API_KEY`,
   `NEXUS_GRPC_HOST`, `NEXUS_GRPC_PORT` — the exact values the remote SDK
   consumes (CLI/SDK parity assertion).

## Missing-surface gate (draft build issues — file only on approval)

### Gap 1 — No CLI prints the resolved deployment-profile contract

- **Missing user workflow**: operator wants to verify, without reading
  source, which bricks/drivers/auth mode the running hub actually has.
  `nexus profile …` manages *connection* profiles, not the deployment
  profile.
- **Proposed surface**: `nexus profile contract` (or
  `nexus status --profile-contract`).
- **Request/response**: no args (uses active connection) →
  JSON `{deployment_profile, bricks[], drivers[], http_surface[],
  grpc_required: bool, auth_mode}`.
- **Tests required**: unit (serialization from `DeploymentProfile`),
  CLI snapshot, parity vs `/api/v2/features`.
- **Benchmark**: not performance-sensitive (control plane).

### Gap 2 — `nexus status` lacks auth/profile detail

- **Missing user workflow**: operator can't confirm auth mode or
  deployment profile from `nexus status`.
- **Proposed surface**: add `deployment_profile` and `auth_mode` keys to
  `nexus status --json`.
- **Request/response**: existing command; additive JSON keys.
- **Tests required**: `nexus status --json` schema test asserting new
  keys; parity with daemon banner.
- **Benchmark**: not performance-sensitive (control plane).

### Gap 3 — No remote-connect preflight

- **Missing user workflow**: a remote client with HTTP reachable but
  gRPC blocked gets a deep stack trace, not an actionable error.
- **Proposed surface**: `nexus doctor remote` (or SDK `connect(...,
  preflight=True)`) that probes HTTP + gRPC and returns a clear
  diagnosis.
- **Request/response**: `--url --api-key` → exit 0 + report, or non-zero
  + "gRPC port N unreachable; set NEXUS_GRPC_PORT".
- **Tests required**: unit (probe logic with mocked sockets), CLI
  failure-path test.
- **Benchmark**: not performance-sensitive (setup path).

> The issue cannot close while a *required* gap above remains untracked.
> Gaps 1 and 3 are required (the guide's correctness assertion and the
> remote workflow depend on them); Gap 2 is recommended. Issues filed via
> `gh issue create`, linked to #4132 and #4121, only after user approval.

## Benchmark classification

| Path | Class | Treatment |
|---|---|---|
| Boot time (cold/warm) | setup path | Captured in gated E2E; reported as guidance range, not a CI gate |
| RSS at idle | setup path | Captured in gated E2E; guidance range |
| `health` / `features` / `Ping` | control plane | Latency asserted in E2E with generous upper bounds |
| Remote connect time | setup path | Recorded once in E2E |
| Steady-state data plane | — | None in this story; explicitly "not performance-sensitive" |

## Test strategy

- **Always-on (CI, no Docker)**: `test_full_profile.py` contract
  assertions; CLI/daemon parity tests for `env`/`status`/banner/auth
  resolution. Pure, fast.
- **Gated E2E (`NEXUS_E2E=1`)**: one module boots the `demo`/`shared`
  stack (FULL deployment profile internally), exercises
  health/features/Ping + remote SDK connect, captures boot/RSS, tears
  down. Reuses `tests/testkit/profiles.py`. Boot fixture written
  profile-agnostic for sibling reuse.
- No new harness package.

## Acceptance-criteria mapping

| Issue criterion | Satisfied by |
|---|---|
| Guide gives start-to-remote-client workflow | `user-guide.md` narrative section + `full-profile.md` |
| Tests cover full feature reporting + remote connection requirements | `test_full_profile.py` + gated E2E |
| Stale `serve`/`minimal`/no-`nexus up` docs reconciled | `docs/paths/daemon-and-remote.md` fix |
| Missing auth/env/status CLI gaps filed as build issues | Missing-surface gate §; filed on approval |
| Matrix links startup/status/auth surface for full | `api-rpc-surface-coverage.yaml` rows |

## Risks

- E2E flakiness on real Docker boot → mitigated by gating + generous
  bounds + teardown fixture.
- Gap issues block closure → surfaced explicitly; user approves filing
  before implementation work elsewhere.
- Vocabulary gap (preset vs profile) could confuse readers → the guide
  has an explicit "preset ↔ deployment profile" mapping table.
