# #4343 Durable Metastore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire a durable redb metastore into the `nexusd-cluster` boot path so the VFS namespace survives kernel restarts, with a Python restart-survival regression test.

**Architecture:** Two PRs. PR 1 (nexi-lab/nexus-vfs): ~20-line change in `rust/profiles/cluster/src/main.rs` — new `--metastore-path` / `NEXUS_METASTORE_PATH` knob defaulting to `<data_dir>/metastore.redb`, wired via the existing `Kernel::set_metastore_path` immediately after `Kernel::new()`, fail-hard on error. The `nexusd-full` binary reuses this exact main.rs, so one fix site covers both shipped binaries. PR 2 (nexi-lab/nexus): bump seven Cargo.toml rev pins + Dockerfile `ARG NEXUS_VFS_REV` (the bump that actually ships the fix), fix a false comment in `kernel_client.py`, add the regression test. TDD across repos: the Python test goes RED against the unfixed binary, GREEN after the Rust fix.

**Tech Stack:** Rust (clap, anyhow, tracing, redb via kernel crate), Python (pytest, KernelClient gRPC), cargo git deps, GitHub PRs via `gh`.

**Spec:** `docs/superpowers/specs/2026-06-10-4343-durable-metastore-design.md` (approved).

**Repos / paths:**
- nexus-vfs clone: `~/nexus-vfs` (currently on stale branch `feat/cluster-optional-driver-s3` with a dirty `Cargo.lock` — Task 1 handles this)
- nexus worktree: `/Users/tafeng/nexus/.claude/worktrees/snuggly-cooking-pearl`, branch `docs-4343-durable-metastore-spec` (spec commits already on it; carries all nexus-side work, renamed before push)

**Environment prerequisites** (verify once, Task 1):
- `protobuf@21` via Homebrew — nexus-vfs raft/transport builds break on system protoc 34. All cargo builds below use `PROTOC="$(brew --prefix protobuf@21)/bin/protoc"`.
- `gh` authenticated for nexi-lab.
- Main-repo venv at `/Users/tafeng/nexus/.venv` (worktree gotcha: do NOT `uv run` in the worktree; if a bare `.venv` exists in the worktree, `rm -rf` it).

---

### Task 1: nexus-vfs branch setup

**Files:** none (git state only)

- [ ] **Step 1: Preserve local state and branch off latest main**

```bash
cd ~/nexus-vfs
git stash push -m "pre-4343 wip (dirty Cargo.lock on feat/cluster-optional-driver-s3)" || true
git fetch origin
git checkout -b fix/4343-durable-metastore origin/main
```

Expected: new branch at `32c4731` ("Merge pull request #41 …") or newer.

- [ ] **Step 2: Verify toolchain prerequisites**

```bash
ls "$(brew --prefix protobuf@21)/bin/protoc" && gh auth status -h github.com 2>&1 | head -3
```

Expected: protoc path prints; gh shows logged-in. If protobuf@21 missing: `brew install protobuf@21`.

---

### Task 2: Python regression test (RED setup — test code first)

**Files:**
- Create: `tests/integration/test_kernel_restart_survival.py`

Work in `/Users/tafeng/nexus/.claude/worktrees/snuggly-cooking-pearl` on branch `docs-4343-durable-metastore-spec`.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_kernel_restart_survival.py`:

```python
"""Restart-survival regression test for issue #4343.

The cluster kernel must keep its VFS namespace (the global
``LocalMetaStore``) in a durable redb under ``NEXUS_DATA_DIR`` — not in
the ``tempfile::tempdir()`` the kernel boots with. Before the fix
(nexus-vfs rev ``32c4731`` and earlier) every kernel restart silently
dropped every file registration: payload bytes survived under
``<data_dir>/root/**`` while ``stat`` returned not-found for everything
written before the boot.

Spawns the real ``nexus-cluster`` binary twice on the same data dir:
write -> restart -> the path must still exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.remote.kernel_client import KernelClient

pytestmark = pytest.mark.integration

PAYLOAD = b"#4343 restart survival probe"
TEST_PATH = "/restart-survival/probe.txt"


def _open_kernel(data_dir: Path) -> KernelClient:
    client = KernelClient(metadata_path=str(data_dir))
    try:
        client.open()
    except FileNotFoundError as exc:
        pytest.skip(
            f"requires the ``nexus-cluster`` binary on PATH "
            f"(KernelClient spawns it). Build it in nexus-vfs with "
            f"``cargo build -p nexus-cluster`` and put "
            f"``target/debug`` on PATH. ({exc})"
        )
    return client


def test_namespace_survives_kernel_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    first = _open_kernel(data_dir)
    try:
        first.sys_write(TEST_PATH, data=PAYLOAD)
        assert first.sys_stat(TEST_PATH) is not None
    finally:
        first.close()

    second = _open_kernel(data_dir)
    try:
        # The registration must survive the restart (#4343: with the
        # tempdir-backed metastore this returned None while the payload
        # bytes sat byte-identical under <data_dir>/root/).
        assert second.sys_stat(TEST_PATH) is not None, (
            "VFS namespace lost across kernel restart — metastore is not durable (#4343)"
        )
        assert second.sys_read_raw(TEST_PATH) == PAYLOAD
        # Negative control: a never-written path must stay absent
        # (guards against walk-import-style blanket re-registration).
        assert second.sys_stat("/restart-survival/never-written.txt") is None
    finally:
        second.close()
```

API notes for the engineer: `KernelClient(metadata_path=...)` stores the path; `.open()` spawns `nexus-cluster` with `NEXUS_DATA_DIR=<metadata_path>`, `NEXUS_NO_TLS=true`, `NEXUS_BOOTSTRAP_MODE=dynamic` on a fresh loopback port (`src/nexus/remote/kernel_client.py:165-204`). `.close()` SIGTERMs and waits — releases the redb lock so the second spawn can open the same file. `sys_stat` returns `None` for not-found. The skip-on-missing-binary pattern mirrors `tests/unit/cli/conftest.py:40-50` (#4133).

- [ ] **Step 2: Build the UNFIXED binary and run the test to verify it fails**

```bash
cd ~/nexus-vfs
PROTOC="$(brew --prefix protobuf@21)/bin/protoc" cargo build -p nexus-cluster
cd /Users/tafeng/nexus/.claude/worktrees/snuggly-cooking-pearl
PYTHONPATH=$PWD/src PATH="$HOME/nexus-vfs/target/debug:$PATH" \
  /Users/tafeng/nexus/.venv/bin/python -m pytest \
  tests/integration/test_kernel_restart_survival.py -v
```

Expected: FAIL at `assert second.sys_stat(TEST_PATH) is not None` with the "#4343" message. (First build takes minutes — vfs workspace from scratch.) If it fails earlier (e.g., on `sys_write`), stop and investigate — that's a different bug, not this plan.

- [ ] **Step 3: Commit the test (red)**

```bash
cd /Users/tafeng/nexus/.claude/worktrees/snuggly-cooking-pearl
git add tests/integration/test_kernel_restart_survival.py
git commit -m "test(#4343): restart-survival regression test — namespace must outlive kernel restart"
```

(Repo pre-commit may need the worktree gotcha: if hooks fail on a bare `.venv`, `rm -rf .venv` and retry.)

---

### Task 3: nexus-vfs fix (GREEN)

**Files:**
- Modify: `~/nexus-vfs/rust/profiles/cluster/src/main.rs` (three edits: CommonArgs field ~line 95, helper impl ~line 212, wiring ~line 516)

- [ ] **Step 1: Add the CommonArgs field**

In `struct CommonArgs`, directly after the `data_dir` field (ends `data_dir: PathBuf,`):

```rust
    /// Durable global metastore (redb) — the kernel's VFS namespace.
    /// File registrations survive restarts only if this lives on
    /// persistent storage. Defaults to `<data_dir>/metastore.redb`.
    #[arg(long, env = "NEXUS_METASTORE_PATH", global = true)]
    metastore_path: Option<PathBuf>,
```

- [ ] **Step 2: Add the resolver helper**

In the existing `impl CommonArgs` block (the one holding `root_fs_path`, ~line 212):

```rust
    fn metastore_db_path(&self) -> PathBuf {
        self.metastore_path
            .clone()
            .unwrap_or_else(|| self.data_dir.join("metastore.redb"))
    }
```

- [ ] **Step 3: Wire the metastore at boot**

Directly after `let kernel = Arc::new(Kernel::new());` (~line 516) and BEFORE the `std::fs::create_dir_all(&root_fs)` / `kernel.mount("/", …)` block:

```rust
    // ── Durable metastore (nexus#4343) ─────────────────────────────
    // Kernel::new() boots with a tempfile-backed LocalMetaStore;
    // without this swap every restart drops the whole VFS namespace.
    // Wire the durable redb BEFORE the first mount or gRPC traffic —
    // registrations made before the swap die with the boot tempdir.
    // Fail the boot if the redb cannot open: a silent tempdir
    // fallback is exactly the data-loss defect this guards against.
    let metastore_db = common.metastore_db_path();
    if let Some(parent) = metastore_db.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("create metastore dir {}", parent.display()))?;
    }
    kernel
        .set_metastore_path(
            metastore_db
                .to_str()
                .context("metastore path must be UTF-8")?,
        )
        .map_err(|e| {
            anyhow::anyhow!(
                "open durable metastore {}: {:?}",
                metastore_db.display(),
                e
            )
        })?;
    tracing::info!(path = %metastore_db.display(), "durable metastore wired");
```

`Context` and `anyhow!` are already imported in this file (`use anyhow::{Context, Result};`).

- [ ] **Step 4: Build, fmt, clippy**

```bash
cd ~/nexus-vfs
PROTOC="$(brew --prefix protobuf@21)/bin/protoc" cargo build -p nexus-cluster
cargo fmt -p nexus-cluster
PROTOC="$(brew --prefix protobuf@21)/bin/protoc" cargo clippy -p nexus-cluster -- -D warnings
```

Expected: clean build, no fmt diff, no clippy warnings.

- [ ] **Step 5: Run the Python test to verify it passes**

```bash
cd /Users/tafeng/nexus/.claude/worktrees/snuggly-cooking-pearl
PYTHONPATH=$PWD/src PATH="$HOME/nexus-vfs/target/debug:$PATH" \
  /Users/tafeng/nexus/.venv/bin/python -m pytest \
  tests/integration/test_kernel_restart_survival.py -v
```

Expected: PASS (1 passed). Also sanity-check the artifact:

```bash
NEXUS_DATA_DIR=/tmp/4343-sanity ~/nexus-vfs/target/debug/nexusd-cluster & sleep 3; kill %1
ls /tmp/4343-sanity/metastore.redb && rm -rf /tmp/4343-sanity
```

Expected: `metastore.redb` exists in the data dir.

- [ ] **Step 6: Commit (nexus-vfs)**

```bash
cd ~/nexus-vfs
git add rust/profiles/cluster/src/main.rs
git commit -m "fix(cluster): wire durable metastore at boot — namespace survived only until restart (nexus#4343)"
```

---

### Task 4: nexus-vfs PR

**Files:** none (GitHub only)

- [ ] **Step 1: Push and open the PR**

Origin must be SSH (osxkeychain shadows the gh credential helper over HTTPS):

```bash
cd ~/nexus-vfs
git remote get-url origin | grep -q '^git@' || git remote set-url origin git@github.com:nexi-lab/nexus-vfs.git
git push -u origin fix/4343-durable-metastore
gh pr create --repo nexi-lab/nexus-vfs \
  --title "fix(cluster): wire durable metastore at boot (nexus#4343)" \
  --body "$(cat <<'EOF'
## Problem

`nexusd-cluster` boots `Kernel::new()` with the tempfile-backed boot
metastore and never calls `set_metastore_path` — the entire VFS
namespace lives in a tempdir and is dropped on every restart
(nexi-lab/nexus#4343, confirmed twice in production). Payload bytes
survive under `<data_dir>/root/**`; every registration vanishes.

## Fix

Profile-level wiring only (no kernel/raft/transport changes):

- New `--metastore-path` / `NEXUS_METASTORE_PATH` knob on `CommonArgs`,
  default `<data_dir>/metastore.redb` (mirrors the `root_fs_path`
  pattern).
- `kernel.set_metastore_path(...)` immediately after `Kernel::new()`,
  before the `/` mount and before gRPC routes — registrations made
  before the swap would die with the boot tempdir.
- Boot fails hard if the redb cannot open. A silent tempdir fallback is
  exactly the data-loss defect this guards against.

`nexusd-full` reuses this `main.rs` verbatim (`[[bin]] path`), so the
fix covers both shipped binaries.

## Testing

Restart-survival regression test (write → restart same data_dir →
`stat` must still resolve) lands in nexi-lab/nexus CI alongside the rev
bump that ships this fix — it fails against rev `32c4731`, passes with
this branch. Local: `cargo build -p nexus-cluster`, fmt, clippy clean.

Refs nexi-lab/nexus#4343.
EOF
)"
```

Expected: PR URL printed; CODEOWNERS auto-assigns @elfenlieds7.

- [ ] **Step 2: Wait for kernel-team review/merge**

Blocked on human review. When merged, capture the merge sha:

```bash
VFS_REV=$(gh pr view <PR-NUMBER> --repo nexi-lab/nexus-vfs --json mergeCommit -q .mergeCommit.oid)
echo "$VFS_REV"
```

All Task 5 commands use `$VFS_REV`.

---

### Task 5: nexus-side changes (rev bumps + comment fix)

**Files:**
- Modify: `Cargo.toml` (7 rev pins), `Cargo.lock` (regenerated)
- Modify: `Dockerfile:99-109` (ARG default + stale comment)
- Modify: `src/nexus/remote/kernel_client.py:880-884`

Work in the worktree on branch `docs-4343-durable-metastore-spec`. Do this task only after the vfs PR merges (`$VFS_REV` known).

- [ ] **Step 1: Bump the seven Cargo.toml pins and regenerate the lock**

```bash
cd /Users/tafeng/nexus/.claude/worktrees/snuggly-cooking-pearl
sed -i '' "s/32c47310e48d449bb5c3f02e48ef12455bf0e8fa/${VFS_REV}/g" Cargo.toml
grep -c "${VFS_REV}" Cargo.toml   # expect: 7
PROTOC="$(brew --prefix protobuf@21)/bin/protoc" cargo check --workspace
git diff --stat Cargo.lock        # expect: nexus-vfs crate revs updated
```

- [ ] **Step 2: Bump the Dockerfile pin and fix its stale comment**

In `Dockerfile`, replace the temporary-pin paragraph and ARG:

Old (~lines 100-109):

```dockerfile
# SSOT once #27 lands on nexus-vfs main: bump the default to the merged-main rev
# (or derive it from the nexus-vfs pin in Cargo.lock). The default below is a
# TEMPORARY integration pin to the unmerged #27 branch tip so the R2 e2e can
# run pre-merge.
ENV CARGO_NET_RETRY=10 \
    CARGO_HTTP_TIMEOUT=120
# Override for edge/CI or a different pin: --build-arg NEXUS_VFS_REV=<sha|tag>
ARG NEXUS_VFS_REV=f4227a21bc8a7546477bc3851bd9407f3579f925
```

New (substitute the real `$VFS_REV` value):

```dockerfile
# Default = nexus-vfs main rev matching the Cargo.toml pins (keep in sync —
# this ARG is what the shipped kernel binary is built from; the Cargo.toml
# pins only sync the plugin/services crates). Includes the durable-metastore
# boot wiring (#4343) — older revs lose the VFS namespace on every restart.
ENV CARGO_NET_RETRY=10 \
    CARGO_HTTP_TIMEOUT=120
# Override for edge/CI or a different pin: --build-arg NEXUS_VFS_REV=<sha|tag>
ARG NEXUS_VFS_REV=<VFS_REV value>
```

- [ ] **Step 3: Fix the false stub comment in kernel_client.py**

Replace `src/nexus/remote/kernel_client.py:880-884`:

Old:

```python
    def set_metastore_path(self, path: str) -> None:
        """Set metastore path — handled at spawn time for subprocess."""
        # For subprocess mode, this was already passed via env.
        # For remote mode, the server manages its own metastore.
        self._metadata_path = path
```

New:

```python
    def set_metastore_path(self, path: str) -> None:
        """Record the data-dir hint used when spawning the kernel.

        Subprocess mode: the binary wires its own durable metastore at
        boot — ``<NEXUS_DATA_DIR>/metastore.redb``, overridable via
        ``NEXUS_METASTORE_PATH`` (#4343). ``path`` only feeds the
        ``NEXUS_DATA_DIR`` spawn env (via ``_resolve_kernel_data_dir``),
        so calling this after ``open()`` has no effect. Remote mode: the
        server manages its own metastore.
        """
        self._metadata_path = path
```

- [ ] **Step 4: Re-verify the regression test against the merged rev**

Rebuild the binary from merged main (not the local branch) so the test proves what ships:

```bash
cd ~/nexus-vfs && git fetch origin && git checkout "$VFS_REV" --detach
PROTOC="$(brew --prefix protobuf@21)/bin/protoc" cargo build -p nexus-cluster
cd /Users/tafeng/nexus/.claude/worktrees/snuggly-cooking-pearl
PYTHONPATH=$PWD/src PATH="$HOME/nexus-vfs/target/debug:$PATH" \
  /Users/tafeng/nexus/.venv/bin/python -m pytest \
  tests/integration/test_kernel_restart_survival.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/tafeng/nexus/.claude/worktrees/snuggly-cooking-pearl
git add Cargo.toml Cargo.lock Dockerfile src/nexus/remote/kernel_client.py
git commit -m "fix(#4343): ship durable-metastore kernel — bump nexus-vfs pins + Dockerfile rev, true up kernel_client stub docs"
```

---

### Task 6: nexus PR + follow-up issue

**Files:** none (GitHub only)

- [ ] **Step 1: Rename branch, rebase on develop, push, open PR**

```bash
cd /Users/tafeng/nexus/.claude/worktrees/snuggly-cooking-pearl
git branch -m fix/4343-durable-metastore-ship
git fetch origin && git rebase origin/develop
git push -u origin fix/4343-durable-metastore-ship
gh pr create --repo nexi-lab/nexus --base develop \
  --title "fix(#4343): ship durable metastore — nexus-vfs rev bump + restart-survival regression test" \
  --body "$(cat <<'EOF'
## Summary

Ships the nexus-vfs durable-metastore fix (nexus-vfs#<VFS-PR-NUMBER>)
and adds the restart-survival regression test.

- Bump seven `Cargo.toml` nexus-vfs pins + `Cargo.lock` to the merged
  rev.
- Bump `Dockerfile` `ARG NEXUS_VFS_REV` — this is the bump that ships
  the fix; the image's kernel binary is `cargo install`ed at this rev.
- `tests/integration/test_kernel_restart_survival.py`: write → restart
  `nexus-cluster` on the same data dir → `stat` must still resolve,
  content reads back byte-identical, never-written path stays absent.
  Fails on rev `32c4731`, passes on the bumped rev.
- `kernel_client.py` `set_metastore_path` docstring trued up (the old
  comment claimed env already carried a metastore path — it never did).
- Design spec: `docs/superpowers/specs/2026-06-10-4343-durable-metastore-design.md`.

Closes #4343.
EOF
)"
```

Note: linear history — rebase, never merge-commit. CI gotchas that may fire: cluster-binary size gate (vfs rev bump changes the installed binary), benchmark gh-pages gating.

- [ ] **Step 2: File the walk-import follow-up issue**

```bash
gh issue create --repo nexi-lab/nexus \
  --title "walk-import: admin command to re-register orphaned payloads under <data_dir>/root/**" \
  --body "$(cat <<'EOF'
Deferred ask 3 from #4343. Instances bitten by the tempdir-metastore
defect have byte-identical payloads on disk that the namespace no
longer references. A `walk-import` admin command re-registering
everything under `<data_dir>/root/**` is the universal recovery path —
downstream ran exactly this via `batch/write` (15,817 files in ~5 min,
DeepBuildAI/koodle#1302). #4343 fixed the defect going forward; this
issue tracks first-class recovery tooling.
EOF
)"
```

- [ ] **Step 3: Update memory**

Write `/Users/tafeng/.claude/projects/-Users-tafeng-nexus/memory/project_issue_4343_durable_metastore.md` with: fix shape (profile wiring, both binaries via shared main.rs), the Dockerfile-ARG-is-the-shipping-bump gotcha, PR numbers, walk-import follow-up issue number. Add an index line to `MEMORY.md`.

---

## Self-review notes

- Spec coverage: wiring knob (T3), fail-hard (T3 S3), both binaries (shared main.rs — no extra task needed), Python-only test (T2), seven pins (T5 S1), Dockerfile ARG (T5 S2), stub comment (T5 S3), local E2E before PRs (T2 S2 red / T3 S5 green), sequencing vfs-first (T4 blocks T5), governance (CODEOWNERS auto-assign), walk-import deferral (T6 S2). No gaps.
- The only values not literal in this plan are `$VFS_REV` / `<VFS-PR-NUMBER>` — unknowable until the kernel team merges PR 1; both are captured by explicit commands (T4 S2).
- Type/name consistency: `metastore_db_path()` used in T3 S3 matches T3 S2; test helper `_open_kernel` matches usage; `sys_stat`/`sys_write`/`sys_read_raw` match `kernel_client.py` (lines 252-340).
