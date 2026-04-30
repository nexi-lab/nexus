# nexus-fs slim package: Rust-kernel contract + bricks decoupling

**Issues:** [#3942](https://github.com/nexi-lab/nexus/issues/3942), [#3943](https://github.com/nexi-lab/nexus/issues/3943), [#3944](https://github.com/nexi-lab/nexus/issues/3944)
**Date:** 2026-04-29
**Status:** Approved (awaiting plan)
**PR:** single bundled PR, branch `fix/3942-3943-3944-slim-rust-contract`

---

## Problem

`nexus-fs` is shipped as the slim wheel. Three ways it's broken on a clean install:

1. **#3942** — `nexus.fs.mount()` unconditionally calls `SQLiteMetastore()`, which imports `nexus_runtime.PyKernel`. `nexus_runtime` is **not** declared as a dep in `packages/nexus-fs/pyproject.toml`. A clean slim install fails before `local://` can mount.
2. **#3943** — local writes traverse `NexusFS.sys_write` → `PyKernel.sys_write`, so even if mount is patched, write/read/mkdir/rename/copy still need the Rust kernel.
3. **#3944** — `src/nexus/backends/connectors/x/connector.py` does a module-level `importlib.import_module("nexus.bricks.search.primitives")`. The slim wheel excludes `**/bricks/**` (per `pyproject.toml:113`), so importing the X connector raises `ModuleNotFoundError`. Six other connectors (gmail, slack, gdrive, calendar, oauth_base, cli/base) have the same problem with `nexus.bricks.auth.*` but defer the imports to call time, so they fail at *use* rather than at *import*.

## Decision: contract change

`nexus-fs` declares `nexus-runtime` as a required runtime dependency. "Slim" stops meaning "no Rust" and starts meaning "no server / no raft / no factory / no bricks tier." Local CRUD via the Rust kernel is guaranteed.

Alternatives considered:

| Option | Effort | Verdict |
|---|---|---|
| **A.** Pure-Python slim — re-introduce a Python metastore + Python local write path + Python glob | weeks; rebuilds kernel surface in Python; risk of write/metastore drift between Python and Rust paths | rejected |
| **B.** `nexus-runtime` as hard dep — declare it, smoke-test the install | days; smallest diff; wheel platform coverage is the main risk | **chosen** |
| **C.** Optional Rust extra — pure-Python by default, `nexus-fs[rust]` for acceleration | 1–2 weeks; doubles the test matrix; ships two slim contracts | rejected |

#3943 dissolves under B: once `nexus-runtime` is a real dep, the existing kernel-backed write path *is* the slim write path. Acceptance is closed by a new install-smoke CI test that proves CRUD on `local://` works in a fresh slim install.

## Architecture

### Boot path (closes #3942)

No code change to the boot flow itself. The fix is the dep declaration:

```diff
 dependencies = [
     "pydantic[email]>=2.0",
     ...
+    "nexus-runtime>=0.10,<0.11",
 ]
```

`nexus-runtime` is the maturin-built wheel from `rust/nexus-cdylib` (PyPI name `nexus-runtime`, module `nexus_runtime`, current version `0.10.0`). Compatible-release pin matches the policy that `nexus-fs` and `nexus-runtime` bump in lock-step.

Update the `_sqlite_meta.py` module docstring to drop the "could not build the Rust kernel" framing — it implied an optional path that never existed.

### Write path (closes #3943)

```
fs.write(path, bytes)
  └─ NexusFS.sys_write(path, bytes, context=LOCAL_CONTEXT)
       └─ PyKernel.sys_write(...)              # Rust, always installed
            └─ backend.write(...)               # local://, s3://, …
```

Already correct under contract B. CRUD is verified by the new install-smoke test (see Testing).

### Bricks carve-out (closes #3944)

Plan iteration discovered that the bricks files connectors depend on (`bricks/auth/oauth/{factory,token_manager,base_provider,...}`, `bricks/auth/{profile,credential_pool}`, `bricks/auth/classifiers/google`, `bricks/search/primitives/`) total ~2000 LOC plus a deep web of internal `bricks/auth/*` deps (cache, types, profile_store, …). A wholesale move-and-reexport would be a multi-week refactor of the whole `bricks/auth/` tier.

Instead, the slim wheel **carves the connector-needed bricks subtrees back into the wheel** via Hatchling `force-include`. The `**/bricks/**` exclude stays (so `bricks/server/`, `bricks/raft/`, `bricks/sync/`, etc. remain off slim), but two specific subtrees ship:

- `nexus/bricks/auth/**` — full subtree; connectors authenticate through it
- `nexus/bricks/search/primitives/**` — pure-Python glob/grep query helpers

Plus the package markers needed for Python to traverse:
- `nexus/bricks/__init__.py`
- `nexus/bricks/search/__init__.py`

`packages/nexus-fs/pyproject.toml` change:

```toml
[tool.hatch.build.targets.wheel.force-include]
"../../src/nexus/bricks/__init__.py" = "nexus/bricks/__init__.py"
"../../src/nexus/bricks/auth" = "nexus/bricks/auth"
"../../src/nexus/bricks/search/__init__.py" = "nexus/bricks/search/__init__.py"
"../../src/nexus/bricks/search/primitives" = "nexus/bricks/search/primitives"
```

`force-include` overrides the broad `**/bricks/**` exclude. No file moves, no re-exports, no connector-import edits. Existing connector code (X module-level import; gmail/slack/gdrive/calendar/oauth_base/cli/base deferred imports) starts working in the slim wheel as-is.

### Slim-tier contract (revised)

"Slim" wheel ships:
- `nexus/{contracts,core,backends,lib,storage,fs,utils}/**` (existing)
- `nexus/bricks/auth/**` and `nexus/bricks/search/primitives/**` (new — for connector auth + glob query helpers)

"Slim" wheel **excludes**: server, raft, factory, fuse, remote, services, grpc, cache, daemon, migrations, network, plugins, proxy, sdk, security, sync, task_manager, tasks, tools, validation, and the `bricks/*` subtrees that aren't auth or search/primitives.

### Connector imports (no change)

X connector keeps its module-level `nexus.bricks.search.primitives` import. The 6 deferred-import connectors keep their `nexus.bricks.auth.oauth.factory` / `nexus.bricks.auth.profile` / `nexus.bricks.auth.classifiers.google` imports. They Just Work in slim because the carve-out includes those paths.

### Failure modes

- `nexus-runtime` not installed → `pip install nexus-fs` fails at install time (declared dep). No silent later ImportError.
- A user installs an extra without optional deps → existing manifest dependency check raises a clear runtime error (unchanged).
- A slim-shipped module reintroduces a `nexus.bricks` import → release-integrity test fails CI.

## Data flow

### Glob path (no change)

```
PathXBackend.glob(pattern)
  └─ nexus.bricks.search.primitives.glob_helpers.glob_filter(...)   # carved into slim
       └─ from nexus._rust_compat import glob_match_bulk             # nexus_runtime, OK
```

### OAuth factory path (no change)

```
connector.authenticate(...)
  └─ importlib.import_module("nexus.bricks.auth.oauth.factory")     # carved into slim
       └─ … existing factory code unchanged
```

No re-export indirection, no `__module__` audit needed. Symbols stay where they are.

## Testing

### Install-smoke (new CI job; closes #3943's AC)

`tests/integration/slim/test_slim_install_smoke.py`:

- Build `packages/nexus-fs` into a wheel.
- Create a fresh venv.
- `pip install <built wheel>` (which transitively pulls `nexus-runtime`).
- Inside the venv, run a script that:
  - imports `nexus.fs`,
  - calls `nexus.fs.mount_sync("local:///tmp/xxx")`,
  - performs write → read → mkdir → rename → copy → delete,
  - asserts content + tree shape.

CI workflow `.github/workflows/slim-wheel-smoke.yml`:

- Trigger: PRs touching `packages/nexus-fs/**`, `src/nexus/fs/**`, `src/nexus/backends/connectors/**`, `src/nexus/lib/**`, `src/nexus/utils/glob.py`.
- Job `build-slim-wheel`: run `python -m build --wheel` from `packages/nexus-fs`.
- Job `smoke-test`: provision fresh venv, install built wheel + `nexus-runtime`, run smoke script.
- Platforms: `ubuntu-latest`, `macos-latest`, Python 3.14 (matching `requires-python` in `packages/nexus-fs/pyproject.toml`). Windows deferred — `nexus-runtime` macOS/Linux wheels are the priority.

### Slim wheel-content audit (new; closes #3944's AC)

`tests/unit/fs/test_slim_wheel_contents.py`:

- Build the slim wheel in a tmpdir.
- Unzip it; assert the file list:
  - **Includes** `nexus/bricks/auth/...`, `nexus/bricks/search/primitives/...`, `nexus/bricks/__init__.py`, `nexus/bricks/search/__init__.py`.
  - **Excludes** `nexus/bricks/server/...`, `nexus/bricks/raft/...`, `nexus/bricks/sync/...`, `nexus/bricks/catalog/...`, plus any other bricks subtree we don't intend to ship.
- Assert the connectors known to depend on bricks (X, gmail, slack, gdrive, calendar, oauth_base, cli/base) import successfully when invoked against the freshly-installed slim wheel (in the smoke-test job's venv).

This replaces the original "import-time block of `nexus.bricks`" integrity test — under the carve-out, slim-shipped modules *do* import `nexus.bricks.*`, and that's expected. The wheel-content audit catches the regression #3944 is really worried about: a future allowlist edit shipping more or less of bricks than we intend.

### Existing tests

- All connector tests (`gmail`, `slack`, `gdrive`, `calendar`, `x`, `oauth_base`) must continue to pass — confirms no behavioral regression.
- `tests/unit/fs/test_slim_external_write.py` may need cleanup of the assertion that "non-external slim writes still surface `_kernel=None` errors" — that condition no longer exists under the new contract.

### Manual verification (PR test plan)

- `cd packages/nexus-fs && pip install -e .` in clean venv → `python -c "import nexus.fs; nexus.fs.mount_sync('local:///tmp/x')"` succeeds.
- `python -c "from nexus.backends.connectors.x.connector import PathXBackend"` succeeds in slim install.
- `python -c "from nexus.backends.connectors.gmail.connector import PathGmailBackend"` succeeds in slim install.

## Risks

1. **`nexus-runtime` wheel platform coverage.** Maturin builds need `manylinux`, `macos-{x86_64,arm64}`, `windows`, `cpython-3.14`. If wheels aren't published for a target platform, `pip install nexus-fs` falls back to a source build (Rust toolchain required). Pre-merge: confirm the wheel index ships for advertised platforms; document the source-build fallback in the README.

2. **Version skew.** `nexus-fs 0.4.9` and `nexus-runtime 0.10.0` aren't pinned together today. We use `nexus-runtime>=0.10,<0.11` (compatible-release). Future bumps require coordinated releases.

3. **Slim wheel weight.** Carving in `bricks/auth/**` (~140 files) and `bricks/search/primitives/**` (3 files) increases slim wheel size. Acceptable: the alternative was a multi-week refactor moving the same code. Wheel-content audit pins what ships.

4. **Bricks tier discipline.** Carving bricks subtrees into the slim package means slim depends on (parts of) the bricks tier. This blurs the previous "slim ships only kernel/lib/contracts" line. Mitigation: the wheel-content audit makes the slim-shipped bricks set explicit; future bricks added to `bricks/auth/` automatically ship to slim — the slim contract is now "slim ships kernel + lib + auth bricks + search primitives." Document this in `packages/nexus-fs/pyproject.toml` next to the `force-include` block.

5. **Single-PR review burden.** ~5 commits: packaging (deps + force-include), smoke test, wheel-content audit, CI workflow, slim_external_write cleanup. Mitigation: clean per-commit messages let reviewers step through.

## Rollback

- **Whole PR:** single `git revert <merge-commit>` brings the slim package back to the current broken state. No data migration. No on-disk schema change.
- **Per-commit:** each commit is independently revertable; lib re-homes can be left in place even if the dep declaration is reverted (re-exports are harmless).

## Decision log

- Picked **B (Rust as hard dep)** over A (pure-Python re-implementation, weeks of work) and C (optional extras, doubles test matrix). Rationale: smallest diff, eliminates the silent-import-failure class.
- For #3944, picked **carve-out (3944b)** over move-and-reexport (3944a). Rationale: discovery during plan-writing showed move-and-reexport requires re-homing ~2000 LOC of `bricks/auth/**` plus its transitive deps — a multi-week refactor of the auth tier. Carve-out is a single `force-include` block in `pyproject.toml`; slim contract becomes "slim ships kernel + lib + auth bricks + search primitives." Wheel-content audit pins the slim-shipped bricks set.
- **Single PR** over a 2-PR split. Rationale: the contract change (`nexus-runtime` dep) and the bricks carve-out are both surgical pyproject edits gated by the same install-smoke + wheel-content tests.
- Sweep all `nexus.bricks` connector imports (not only X). Rationale: the carve-out picks up all 7 connectors at once — auth subtree covers gmail/slack/gdrive/calendar/oauth_base/cli/base; search/primitives covers X.

## Out of scope

- `nexus-ai-fs` packaging (unchanged).
- Restoring a Python-only metastore.
- Connector behavior or feature changes.
- Windows CI for slim-wheel-smoke (deferred until `nexus-runtime` ships Windows wheels and the team confirms support).
