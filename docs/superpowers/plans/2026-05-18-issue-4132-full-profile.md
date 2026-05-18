# Full Hub Startup, Auth, Remote Client & Profile Contract — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Document and test the FULL deployment-profile contract — shared-hub startup, static/database auth, and remote client connection — with a deployment page, a reconciled user-guide narrative, contract + parity tests, a gated E2E, matrix wiring, and a tracked missing-surface backlog.

**Architecture:** No product behavior changes. We lock the existing `DeploymentProfile.FULL` contract with characterization tests (source of truth the docs cite), add a `docs/deployment/full-profile.md` reference page mirroring `sandbox-profile.md`, reconcile the stale `daemon-and-remote.md`, wire the surface-coverage matrix curation fields, and add one `NEXUS_E2E=1`-gated Docker-boot test. Missing-surface gaps are drafted as issue-body files and filed only on user approval.

**Tech Stack:** Python 3.x, pytest (`pytest.mark.integration`), Click CLI, Docker Compose, YAML surface-coverage matrix (`scripts/surface_coverage/`), Markdown docs.

---

## Spec

Source spec: `docs/superpowers/specs/2026-05-18-issue-4132-full-profile-design.md`

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `tests/unit/core/test_full_profile.py` | Create | Characterize FULL brick/driver contract; superset over LITE; excludes federation |
| `tests/unit/daemon/test_full_profile_daemon.py` | Create | `nexusd --profile remote` rejection; `--profile full` banner line |
| `tests/unit/cli/test_full_profile_parity.py` | Create | `nexus env --json` / `nexus status --json` keys = SDK-consumed values |
| `docs/deployment/full-profile.md` | Create | FULL contract reference page (mirrors `sandbox-profile.md`) |
| `docs/guides/user-guide.md` | Modify (§4) | Reconcile "Start A Shared Server" narrative; link profile page; preset↔profile table |
| `docs/paths/daemon-and-remote.md` | Modify | Replace `--profile minimal` → `full`; add `nexus up` path; keep raw-`nexusd`; state gRPC requirement |
| `docs/architecture/api-rpc-surface-coverage.yaml` (via curation input) | Modify | Set `owning_issue`/`correctness_test`/`usage_example` for startup/auth/env/status ops |
| `docs/architecture/api-rpc-surface-gaps.yaml` | Modify | Add 3 gap entries (no GitHub issues yet) |
| `tests/integration/test_full_profile_boot.py` | Create | `NEXUS_E2E=1`-gated real-Docker boot: health/features/Ping + remote SDK connect + boot/RSS capture |
| `docs/superpowers/gaps/4132-gap-*.md` | Create | Draft GitHub issue bodies for user review before filing |

---

### Task 1: Characterize the FULL brick/driver contract

**Files:**
- Create: `tests/unit/core/test_full_profile.py`

- [ ] **Step 1: Write the test**

```python
"""Characterization tests for DeploymentProfile.FULL (Issue #4132).

These lock the FULL contract that docs/deployment/full-profile.md cites.
FULL = LITE bricks + the full feature set, EXCLUDING federation
(federation is cloud = full ∪ {federation}).
"""

from nexus.contracts.deployment_profile import (
    BRICK_ACCESS_MANIFEST,
    BRICK_FEDERATION,
    BRICK_LLM,
    BRICK_MCP,
    BRICK_PAY,
    BRICK_SEARCH,
    BRICK_SNAPSHOT,
    BRICK_VERSIONING,
    BRICK_WORKSPACE,
    DRIVER_GCS,
    DRIVER_GDRIVE,
    DRIVER_REMOTE,
    DRIVER_S3,
    DeploymentProfile,
)


class TestFullProfileContract:
    def test_enum_value(self) -> None:
        assert DeploymentProfile.FULL == "full"
        assert DeploymentProfile("full") is DeploymentProfile.FULL

    def test_superset_over_lite(self) -> None:
        full = DeploymentProfile.FULL.default_bricks()
        lite = DeploymentProfile.LITE.default_bricks()
        assert lite.issubset(full)

    def test_includes_feature_bricks(self) -> None:
        bricks = DeploymentProfile.FULL.default_bricks()
        for b in (
            BRICK_SEARCH,
            BRICK_PAY,
            BRICK_LLM,
            BRICK_MCP,
            BRICK_WORKSPACE,
            BRICK_SNAPSHOT,
            BRICK_VERSIONING,
            BRICK_ACCESS_MANIFEST,
        ):
            assert b in bricks, f"{b} must be enabled in FULL"

    def test_excludes_federation(self) -> None:
        # FULL excludes federation; CLOUD = FULL ∪ {federation}
        assert BRICK_FEDERATION not in DeploymentProfile.FULL.default_bricks()
        assert BRICK_FEDERATION in DeploymentProfile.CLOUD.default_bricks()

    def test_cloud_is_full_plus_federation(self) -> None:
        full = DeploymentProfile.FULL.default_bricks()
        cloud = DeploymentProfile.CLOUD.default_bricks()
        assert cloud == full | {BRICK_FEDERATION}

    def test_drivers_include_cloud_storage(self) -> None:
        drivers = DeploymentProfile.FULL.default_drivers()
        for d in (DRIVER_S3, DRIVER_GCS, DRIVER_GDRIVE, DRIVER_REMOTE):
            assert d in drivers
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/unit/core/test_full_profile.py -v`
Expected: PASS (contract already exists in `deployment_profile.py`; this locks it against regression). If `test_cloud_is_full_plus_federation` fails, the source contract changed — STOP and reconcile the spec, do not weaken the test.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/core/test_full_profile.py
git commit -m "test(#4132): characterize FULL brick/driver contract"
```

---

### Task 2: Daemon denial + banner characterization

**Files:**
- Create: `tests/unit/daemon/test_full_profile_daemon.py`
- Reference: `src/nexus/daemon/main.py` (remote-profile guard ~line 352; banner `  Profile: {deployment_profile}`)

- [ ] **Step 1: Write the test**

```python
"""Daemon-side FULL profile behavior (Issue #4132).

- `nexusd --profile remote` is rejected (a daemon cannot be a thin
  client of another daemon).
- The startup banner echoes the resolved profile.
"""

from click.testing import CliRunner

from nexus.daemon.main import main as nexusd_main


def test_remote_profile_is_rejected() -> None:
    runner = CliRunner()
    result = runner.invoke(nexusd_main, ["--profile", "remote"])
    assert result.exit_code != 0
    assert "cannot run with profile='remote'" in result.output


def test_full_profile_banner(tmp_path) -> None:
    runner = CliRunner()
    # --check-only style: invoke far enough to print the banner then fail
    # fast. If main has no dry-run, assert on output captured before the
    # blocking serve loop via a short timeout helper instead.
    result = runner.invoke(
        nexusd_main,
        ["--profile", "full", "--data-dir", str(tmp_path), "--dry-run"],
    )
    assert "Profile: full" in result.output
```

- [ ] **Step 2: Run test to verify the remote-rejection passes**

Run: `pytest tests/unit/daemon/test_full_profile_daemon.py::test_remote_profile_is_rejected -v`
Expected: PASS.

- [ ] **Step 3: Verify the banner test; adapt if `--dry-run` is absent**

Run: `pytest tests/unit/daemon/test_full_profile_daemon.py::test_full_profile_banner -v`
Expected: PASS. If FAIL because `nexusd` has no `--dry-run` flag, replace `test_full_profile_banner` with an assertion that the banner-building code path is exercised by importing the helper that emits it. Concretely, grep first:

Run: `grep -n "dry.run\|check.only\|Profile: " src/nexus/daemon/main.py`
Then either use the discovered no-op flag, or delete `test_full_profile_banner` and instead assert the remote-rejection output already proves banner/guard wiring (the guard runs before the banner). Keep only tests that pass without a live serve loop.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/daemon/test_full_profile_daemon.py
git commit -m "test(#4132): nexusd FULL banner + remote-profile rejection"
```

---

### Task 3: CLI/SDK parity test for env/status JSON

**Files:**
- Create: `tests/unit/cli/test_full_profile_parity.py`
- Reference: `tests/unit/cli/test_env_cmd.py`, `tests/unit/cli/test_status.py`, `CLI.md` env var table

- [ ] **Step 1: Inspect the existing env command output shape**

Run: `pytest tests/unit/cli/test_env_cmd.py -v` and
`grep -n "NEXUS_URL\|NEXUS_GRPC\|--json\|def cmd" src/nexus/cli/commands/stack.py | head`
Expected: confirms the `nexus env --json` key names. Use the exact keys observed; do not invent.

- [ ] **Step 2: Write the parity test**

```python
"""CLI/SDK parity for the remote-connect contract (Issue #4132).

`nexus env --json` must emit exactly the connection values the remote
SDK consumes: NEXUS_URL, NEXUS_API_KEY, NEXUS_GRPC_HOST, NEXUS_GRPC_PORT.
The remote SDK path needs gRPC, not just the HTTP URL.
"""

import json

from click.testing import CliRunner

from nexus.cli.main import cli


def test_env_json_emits_grpc_and_http(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["init", "--preset", "shared"])
    result = runner.invoke(cli, ["env", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    for key in ("NEXUS_URL", "NEXUS_API_KEY", "NEXUS_GRPC_HOST", "NEXUS_GRPC_PORT"):
        assert key in payload, f"{key} missing from `nexus env --json`"
    # gRPC is required for the remote SDK path — the contract the guide asserts.
    assert payload["NEXUS_GRPC_PORT"]
```

- [ ] **Step 3: Run; align keys with actual output**

Run: `pytest tests/unit/cli/test_full_profile_parity.py -v`
Expected: PASS. If a key name differs (e.g. nested under `env`), adjust the assertion to the real shape discovered in Step 1 — keep the intent (HTTP + gRPC + key all present).

- [ ] **Step 4: Commit**

```bash
git add tests/unit/cli/test_full_profile_parity.py
git commit -m "test(#4132): nexus env --json HTTP+gRPC parity for remote SDK"
```

---

### Task 4: FULL profile deployment page

**Files:**
- Create: `docs/deployment/full-profile.md`
- Reference: `docs/deployment/sandbox-profile.md` (structure to mirror), Task 1 test (cited as the correctness check)

- [ ] **Step 1: Write the page**

Create `docs/deployment/full-profile.md` with these sections (fill from verified spec facts):

```markdown
# FULL deployment profile

Nexus's `full` profile is the all-feature shared hub for a team:
PostgreSQL + Dragonfly + Zoekt, the complete brick set, and local
inference. Use it for a shared node that exposes the full CLI/RPC
surface; use `sandbox` for per-agent clients that connect to it.

## Three things called "profile" (read this first)

| Term | Where | What it controls |
|---|---|---|
| Docker Compose profile (`core`, `cache`) | `nexus up` / `docker-compose.yml` | Which containers start |
| CLI connection profile | `nexus profile use <name>` (`~/.nexus/config.yaml`) | Which hub the CLI talks to |
| Deployment profile (`full`) | `nexusd --profile full` / `NEXUS_PROFILE` | Which bricks/drivers are enabled |

`nexus up` runs the FULL deployment profile because
`docker-compose.yml` sets `NEXUS_PROFILE=full`. No `nexus init` preset
is literally named `full`; `shared` and `demo` presets both run FULL.

## What you get

| Surface | FULL |
|---|---|
| Storage | PostgreSQL |
| Cache | Dragonfly / Redis |
| Keyword search | BM25S + Zoekt |
| Bricks | LITE + search, pay, llm, mcp, workspace, snapshot, versioning, identity, delegation, share_link, portability, task_manager, observability, … (see contract test) |
| Federation | OFF (that is the `cloud` profile) |
| Auth | static (`NEXUS_API_KEY`) or database (`DatabaseAPIKeyAuth`) |
| Remote clients | `profile=remote` SDK; requires gRPC, not just HTTP |

## Running

### Via the stack (recommended)

\`\`\`bash
nexus init --preset shared
nexus up
eval $(nexus env)
nexus status
\`\`\`

### Via the daemon directly

\`\`\`bash
nexusd --profile full --host 0.0.0.0 --port 2026 \
  --data-dir ./nexus-data --auth-type static --api-key "$NEXUS_API_KEY"
\`\`\`

`nexusd --profile remote` is rejected: a daemon cannot be a thin
client of another daemon.

## Auth

- **static**: `--api-key` / `NEXUS_API_KEY` / `NEXUS_API_KEY_FILE`.
  Request without a key → 401; with key → 200.
- **database**: `--auth-type database` + `--database-url` (or
  `POSTGRES_URL`) → `DatabaseAPIKeyAuth`. Use for multi-user key
  issuance/revocation.

## Remote client

\`\`\`python
from nexus.sdk import connect
nx = connect(config={"profile": "remote",
                     "url": "http://hub:2026",
                     "api_key": "..."})
\`\`\`

Set `NEXUS_GRPC_PORT` if the server's gRPC port is non-default. The
HTTP URL alone is not sufficient.

## Correctness check you can run

The FULL contract is locked by
`tests/unit/core/test_full_profile.py`. Run:

\`\`\`bash
pytest tests/unit/core/test_full_profile.py -v
\`\`\`

## Benchmark guidance

Boot time and idle RSS are setup-path metrics, not CI gates; the FULL
stack (PostgreSQL + Dragonfly + Zoekt) targets multi-GB RSS and a
15–60 s boot. `health` / `features` / `Ping` are control-plane calls
with sub-100 ms expectations on a warm hub. There is no steady-state
data-plane hot path in the startup story.

## Troubleshooting

- Remote SDK hangs / connection refused: gRPC port unreachable — set
  `NEXUS_GRPC_PORT`, confirm `nexus status` shows gRPC healthy.
- 401 from every call: static auth with no `NEXUS_API_KEY`, or
  database auth with no issued key.
```

- [ ] **Step 2: Verify referenced test path exists**

Run: `test -f tests/unit/core/test_full_profile.py && echo OK`
Expected: `OK` (created in Task 1).

- [ ] **Step 3: Commit**

```bash
git add docs/deployment/full-profile.md
git commit -m "docs(#4132): FULL deployment profile reference page"
```

---

### Task 5: Reconcile the user-guide narrative + link

**Files:**
- Modify: `docs/guides/user-guide.md` (§4 "Start A Shared Server", lines ~278–394)

- [ ] **Step 1: Read the current §4**

Run: `sed -n '278,395p' docs/guides/user-guide.md`
Expected: see existing steps (simple dev server, connect from another terminal, save CLI profile, remote Python client, database auth).

- [ ] **Step 2: Insert a profile-pointer + preset↔profile table at the top of §4**

After the `## 4. Start A Shared Server` heading, add:

```markdown
> This walkthrough runs the **FULL deployment profile**. For the full
> brick/driver contract, auth modes, and the three different things
> called "profile", see [FULL deployment profile](../deployment/full-profile.md).

| `nexus init --preset` | Docker stack | Deployment profile |
|---|---|---|
| `local` | none (embedded) | embedded/lite |
| `shared` | postgres+dragonfly+zoekt | **full** |
| `demo` | shared + seed data | **full** |
```

- [ ] **Step 3: Ensure the remote-client step states the gRPC requirement**

In "Step 4: Connect with the remote Python client", confirm/add a line:

```markdown
> The remote SDK path requires gRPC, not only the HTTP URL. If the
> server's gRPC port is non-default, set `NEXUS_GRPC_PORT` in the
> client environment.
```

- [ ] **Step 4: Grep for stale vocabulary in the guide**

Run: `grep -n "nexus serve\|--profile minimal\|profile=minimal" docs/guides/user-guide.md`
Expected: no matches. If any appear, replace `nexus serve` → `nexusd` or `nexus up`, and `minimal` → `full`/`lite` as context dictates.

- [ ] **Step 5: Commit**

```bash
git add docs/guides/user-guide.md
git commit -m "docs(#4132): reconcile user-guide §4 with FULL profile + preset↔profile map"
```

---

### Task 6: Fix the stale `daemon-and-remote.md`

**Files:**
- Modify: `docs/paths/daemon-and-remote.md`

- [ ] **Step 1: Replace the `minimal` server example**

Replace the `## Server` code block:

```markdown
## Server

Run the daemon directly:

\`\`\`bash
export NEXUS_GRPC_PORT=2126
nexusd --profile full --host 127.0.0.1 --port 2026 \
  --data-dir ./nexus-data --auth-type static --api-key dev-key
\`\`\`

Or run the managed stack (FULL profile, PostgreSQL/Dragonfly/Zoekt):

\`\`\`bash
nexus init --preset shared && nexus up && eval $(nexus env)
\`\`\`

> `minimal` is not a deployment profile. Valid profiles: `embedded`,
> `lite`, `sandbox`, `full`, `cloud`, `cluster`, `remote` (and `remote`
> cannot run as a daemon). See
> [FULL deployment profile](../deployment/full-profile.md).
```

- [ ] **Step 2: Verify no `minimal` remains**

Run: `grep -n "minimal\|nexus serve" docs/paths/daemon-and-remote.md`
Expected: no matches (the Trust Notes gRPC text stays).

- [ ] **Step 3: Commit**

```bash
git add docs/paths/daemon-and-remote.md
git commit -m "docs(#4132): reconcile stale daemon-and-remote.md (minimal->full, add nexus up)"
```

---

### Task 7: Wire the surface-coverage matrix curation fields

**Files:**
- Modify: surface-coverage curation input for `docs/architecture/api-rpc-surface-coverage.yaml`

- [ ] **Step 1: Find the curation mechanism (rows are generated — do NOT hand-edit generated YAML)**

Run: `sed -n '1,80p' scripts/surface_coverage/merge.py` and
`grep -rn "owning_issue\|correctness_test\|usage_example\|curat" scripts/surface_coverage/*.py | head`
Expected: identify the file/overlay where `owning_issue` / `correctness_test` / `usage_example` are injected (a curation overlay or annotations source), not the generated `api-rpc-surface-coverage.yaml`.

- [ ] **Step 2: Set curation fields for startup/auth/env/status operations**

For the operations covering daemon startup, auth, `nexus env`, and `nexus status`, set in the curation source:
- `owning_issue: 4132`
- `correctness_test: tests/unit/core/test_full_profile.py` (or the parity test for env/status ops)
- `usage_example: docs/deployment/full-profile.md`
- `perf_class: control_plane` for health/features/Ping; `setup` for boot/connect

- [ ] **Step 3: Regenerate and verify the matrix reflects FULL wiring**

Run: `python scripts/gen_api_surface_coverage.py` (or the documented regenerate entrypoint discovered in Step 1)
Then: `grep -n "owning_issue: 4132" docs/architecture/api-rpc-surface-coverage.yaml | head`
Expected: matches for the curated operations; `full` profile column present.

- [ ] **Step 4: Run the matrix freshness/render tests**

Run: `pytest tests/architecture/ -q`
Expected: PASS (freshness gate is warn-only per #4161; render must pass).

- [ ] **Step 5: Commit**

```bash
git add scripts/surface_coverage docs/architecture/api-rpc-surface-coverage.yaml docs/architecture/api-rpc-surface-coverage.html
git commit -m "docs(#4132): wire FULL startup/auth/env/status into surface-coverage matrix"
```

---

### Task 8: Record missing-surface gaps in gaps.yaml

**Files:**
- Modify: `docs/architecture/api-rpc-surface-gaps.yaml`

- [ ] **Step 1: Append the 3 gap entries (schema mirrors existing entries)**

```yaml
  - id: profile.contract_cli
    module: cli
    summary: "`nexus profile contract` (or `nexus status --profile-contract`): print resolved deployment profile, bricks[], drivers[], http_surface[], grpc_required, auth_mode as JSON."
    wanted_why: "Operators cannot verify the running hub's FULL contract without reading source; the user-guide correctness assertion needs a user-runnable command. Issue #4132 Gap 1 (required)."

  - id: status.auth_profile_detail
    module: cli
    summary: "Add `deployment_profile` and `auth_mode` keys to `nexus status --json`."
    wanted_why: "`nexus status` cannot confirm auth mode or deployment profile. Issue #4132 Gap 2 (recommended)."

  - id: remote.connect_preflight
    module: remote
    summary: "`nexus doctor remote` / SDK preflight: probe HTTP + gRPC reachability, return an actionable error instead of a deep stack trace."
    wanted_why: "Remote clients with gRPC blocked get an opaque failure. Issue #4132 Gap 3 (required)."
```

- [ ] **Step 2: Validate the YAML**

Run: `python -c "import yaml,sys; yaml.safe_load(open('docs/architecture/api-rpc-surface-gaps.yaml'))" && echo OK`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/api-rpc-surface-gaps.yaml
git commit -m "docs(#4132): record 3 missing-surface gaps (profile-contract, status-detail, remote-preflight)"
```

---

### Task 9: Gated real-Docker E2E + benchmark capture

**Files:**
- Create: `tests/integration/test_full_profile_boot.py`
- Reference: `tests/integration/approvals/test_grpc_server.py` (gating pattern), `tests/testkit/profiles.py`

- [ ] **Step 1: Write the gated E2E**

```python
"""FULL profile real-boot E2E (Issue #4132).

Gated: only runs with NEXUS_E2E=1 (boots a real Docker stack:
PostgreSQL + Dragonfly + Zoekt). Captures boot/RSS as guidance, not
CI gates; asserts control-plane calls with generous bounds.
"""

import os
import time

import pytest

pytestmark = pytest.mark.integration

requires_e2e = pytest.mark.skipif(
    os.environ.get("NEXUS_E2E") != "1",
    reason="FULL boot E2E requires NEXUS_E2E=1 (real Docker stack)",
)


@requires_e2e
def test_full_stack_boots_and_serves(full_stack):
    """full_stack fixture: nexus init --preset shared; nexus up; yield env; nexus down."""
    t0 = time.monotonic()
    health = full_stack.http_get("/health")
    boot_s = time.monotonic() - t0
    assert health.status_code == 200
    features = full_stack.http_get("/api/v2/features")
    assert features.status_code == 200
    body = features.json()
    assert body  # FULL reports a non-empty feature set
    # Guidance only — recorded, not asserted as a gate.
    print(f"[bench] first-health latency ~ {boot_s:.2f}s")


@requires_e2e
def test_remote_sdk_connect(full_stack):
    from nexus.sdk import connect

    nx = connect(config={
        "profile": "remote",
        "url": full_stack.url,
        "api_key": full_stack.api_key,
    })
    assert nx is not None
    # gRPC-backed op proves the remote path, not just HTTP reachability.
    nx.ls("/")


@requires_e2e
def test_remote_sdk_without_grpc_fails_clearly(full_stack, monkeypatch):
    from nexus.sdk import connect

    monkeypatch.setenv("NEXUS_GRPC_PORT", "1")  # unreachable
    with pytest.raises(Exception):
        connect(config={
            "profile": "remote",
            "url": full_stack.url,
            "api_key": full_stack.api_key,
        }).ls("/")
```

- [ ] **Step 2: Add the `full_stack` fixture (profile-agnostic, sibling-reusable)**

Inspect for an existing stack fixture first:

Run: `grep -rn "def full_stack\|nexus up\|preset shared\|@pytest.fixture" tests/integration/conftest.py tests/conftest.py 2>/dev/null | head`

If none exists, add to `tests/integration/conftest.py` a `full_stack` fixture that: `nexus init --preset shared` in a tmp dir, `nexus up --timeout 300`, parses `nexus env --json` into `.url/.api_key/.http_get`, yields, and `nexus down --volumes` in teardown. Keep it profile-parameterizable (accept a `preset` arg defaulting to `shared`) so #4133–#4138 reuse it.

- [ ] **Step 3: Verify the gate skips by default**

Run: `pytest tests/integration/test_full_profile_boot.py -v`
Expected: 3 SKIPPED ("requires NEXUS_E2E=1").

- [ ] **Step 4: Run the E2E once locally with Docker (manual gate)**

Run: `NEXUS_E2E=1 pytest tests/integration/test_full_profile_boot.py -v -s`
Expected: 3 PASSED; `[bench]` line printed. If Docker is unavailable in this environment, record that the gated run was not executed here and must run in an environment with Docker — do NOT claim it passed.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_full_profile_boot.py tests/integration/conftest.py
git commit -m "test(#4132): gated FULL real-boot E2E (health/features/Ping + remote SDK)"
```

---

### Task 10: Draft gap issue bodies; file only on user approval

**Files:**
- Create: `docs/superpowers/gaps/4132-gap-1-profile-contract.md`
- Create: `docs/superpowers/gaps/4132-gap-2-status-detail.md`
- Create: `docs/superpowers/gaps/4132-gap-3-remote-preflight.md`

- [ ] **Step 1: Write each draft using the spec's missing-surface template**

Each file: title, missing user workflow, proposed CLI/RPC signature, request/response shape, tests required before docs claim support, benchmark classification, and `Parent: #4132 / Epic: #4121`. Copy the exact text from the spec's "Missing-surface gate" section (Gap 1/2/3).

- [ ] **Step 2: Commit the drafts**

```bash
git add docs/superpowers/gaps/4132-gap-1-profile-contract.md docs/superpowers/gaps/4132-gap-2-status-detail.md docs/superpowers/gaps/4132-gap-3-remote-preflight.md
git commit -m "docs(#4132): draft missing-surface gap issue bodies (unfiled)"
```

- [ ] **Step 3: STOP — request user approval before filing**

Do not run `gh issue create`. Present the 3 drafts to the user. Only after explicit approval, for each approved draft run:

```bash
gh issue create --repo nexi-lab/nexus --title "<title>" --body-file docs/superpowers/gaps/4132-gap-N-<slug>.md --label "component: api"
```

Then record the returned issue number into the matching `gap_issue:`
field in `docs/architecture/api-rpc-surface-gaps.yaml`, commit, and add
the link as a comment on #4132. The issue cannot close while required
gaps 1 and 3 are untracked.

---

### Task 11: Final verification & issue cross-link

- [ ] **Step 1: Run the full always-on suite for this story**

Run: `pytest tests/unit/core/test_full_profile.py tests/unit/daemon/test_full_profile_daemon.py tests/unit/cli/test_full_profile_parity.py tests/architecture/ -q`
Expected: all PASS (E2E remains skipped without `NEXUS_E2E=1`).

- [ ] **Step 2: Confirm no stale vocabulary remains in touched docs**

Run: `grep -rn "nexus serve\|--profile minimal\|profile=minimal" docs/guides/user-guide.md docs/paths/daemon-and-remote.md docs/deployment/full-profile.md`
Expected: no matches.

- [ ] **Step 3: Verify acceptance-criteria mapping**

Re-read the spec's "Acceptance-criteria mapping" table; confirm each row has a landed artifact. List any gap explicitly to the user.

- [ ] **Step 4: Post a summary comment on #4132**

After user approval (per Task 10), comment on #4132 linking the deployment page, user-guide section, tests, matrix wiring, and filed gap issues; note that required gaps 1 & 3 block closure until resolved.

---

## Self-Review

- **Spec coverage:** deployment page (T4) ✓, user-guide narrative (T5) ✓, stale-doc reconcile (T6) ✓, contract tests (T1/T2/T3) ✓, gated E2E + benchmarks (T9) ✓, matrix wiring (T7) ✓, missing-surface gate (T8/T10) ✓, acceptance mapping (T11) ✓.
- **Placeholders:** none — every doc/test step has full content; the two investigation steps (T2.3, T7.1) are concrete commands with expected output and a defined fallback, not "TBD".
- **Type consistency:** `DeploymentProfile.FULL`, `default_bricks()`, `default_drivers()`, `BRICK_*`/`DRIVER_*` constants match `deployment_profile.py`; `full_stack` fixture name consistent across T9 steps; gap ids consistent between T8 (gaps.yaml) and T10 (draft files).
- **Risk acknowledged:** T9.4 explicitly forbids claiming E2E pass without Docker; T2.3 forbids weakening the remote-rejection test; T7.1 forbids hand-editing generated YAML.
