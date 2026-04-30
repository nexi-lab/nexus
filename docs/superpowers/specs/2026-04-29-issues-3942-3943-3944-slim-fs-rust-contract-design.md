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

### Bricks decoupling (closes #3944)

Slim-needed pure-Python helpers move out of `nexus.bricks.*` into slim-shipped homes; `nexus.bricks.*` keeps its public names as thin re-exports for full-runtime back-compat.

New units (one focused module per file):

| New module (slim-shipped) | Owns | Old home (becomes re-export) |
|---|---|---|
| `nexus/utils/glob.py` | `glob_match`, `glob_filter`, `extract_static_prefix` | `nexus.bricks.search.primitives.glob_helpers` |
| `nexus/lib/auth/oauth_factory.py` | `create_token_manager` factory surface | `nexus.bricks.auth.oauth.factory` |
| `nexus/lib/auth/profile.py` | `AuthProfile` | `nexus.bricks.auth.profile` |
| `nexus/lib/auth/classifiers/google.py` | `classify_google_error` | `nexus.bricks.auth.classifiers.google` |
| `nexus/lib/auth/credential_pool.py` | `CredentialPool` | `nexus.bricks.auth.credential_pool` |
| `nexus/lib/auth/oauth/token_manager.py` | `TokenManager` | `nexus.bricks.auth.oauth.token_manager` |

`nexus.lib.oauth` already exists in the slim-shipped tree with overlapping concerns (providers, protocol, pkce, discovery). The new `nexus/lib/auth/*` modules nest under `nexus.lib` but live in a sibling `auth/` subpackage to avoid colliding with the existing `nexus.lib.oauth.*` names. The implementation plan picks the final landing path during plan-writing once it greps both trees for collisions.

The bricks files listed above pull transitive imports (cache, types, constants, profile_store, etc.). The implementation plan must:

1. Trace each move target's transitive imports.
2. Decide per-import whether to move the dep, leave it in bricks (and add a slim re-import), or refactor it out of the slim path entirely.
3. Confirm the resulting `nexus.lib.*` subtree imports cleanly with `nexus.bricks.*` blocked (the integrity test enforces this).

Each `nexus.bricks.*` module becomes:

```python
# nexus/bricks/search/primitives/glob_helpers.py
from nexus.utils.glob import glob_match, glob_filter, extract_static_prefix  # noqa: F401
__all__ = ["glob_match", "glob_filter", "extract_static_prefix"]
```

Connectors (`nexus.backends.connectors.{x,gmail,slack,gdrive,calendar,oauth_base,cli/base,oauth}`) update imports to point at `nexus.utils.*` / `nexus.lib.*` directly, never at `nexus.bricks.*`.

`packages/nexus-fs/pyproject.toml` already includes `nexus/lib/**` in the wheel allowlist; the new `nexus/utils/glob.py` is also covered by the existing `"nexus/utils/__init__.py"` + targeted file pattern — extend the allowlist:

```diff
     "nexus/utils/__init__.py",
     "nexus/utils/edit_engine.py",
+    "nexus/utils/glob.py",
```

### Failure modes

- `nexus-runtime` not installed → `pip install nexus-fs` fails at install time (declared dep). No silent later ImportError.
- A user installs an extra without optional deps → existing manifest dependency check raises a clear runtime error (unchanged).
- A slim-shipped module reintroduces a `nexus.bricks` import → release-integrity test fails CI.

## Data flow

### Glob path (after PR)

```
PathXBackend.glob(pattern)
  └─ from nexus.utils.glob import glob_filter
  └─ glob_filter(paths, include, exclude)
       └─ from nexus._rust_compat import glob_match_bulk     # nexus_runtime, OK
nexus.bricks.search.primitives.glob_helpers
  └─ from nexus.utils.glob import *                          # full-runtime back-compat
```

### OAuth factory path (after PR)

```
connector.authenticate(...)
  └─ from nexus.lib.auth.oauth_factory import create_token_manager
nexus.bricks.auth.oauth.factory
  └─ from nexus.lib.auth.oauth_factory import create_token_manager  # re-export
```

### Re-export contract

- `nexus.bricks.*` modules listed above keep their public names; full-runtime callers see no diff.
- Internal callers inside bricks that do `from .factory import create_token_manager` keep working — the re-export is module-level, not a wildcard hack.
- `is`-identity is preserved (the same function object is reachable from both paths).
- `__module__` on these symbols moves to the new path. Any caller that introspects `inspect.getmodule(...)` will see the new path. Pre-merge audit:
  - `rg -n '__module__|inspect\.getmodule' src/ tests/` over the names `glob_match`, `glob_filter`, `extract_static_prefix`, `create_token_manager`, `AuthProfile`, `CredentialPool`, `TokenManager`, `classify_google_error`.
  - For each hit, decide: keep (compares to new path), update (if asserting old path), or drop (if the assertion is incidental).

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

### Slim release-integrity (new; closes #3944's AC)

`tests/unit/fs/test_slim_imports.py`:

- Discover slim-shipped backend modules via `pkgutil.walk_packages(nexus.backends, ...)`.
- For each module, spawn a subprocess (subprocess isolation matters — once `nexus.bricks` is imported by another test in the same process, the test is meaningless).
- In the subprocess, install a sentinel meta-path finder that raises `ImportError` for any name starting with `nexus.bricks`.
- Import the connector module. Assert success.
- Allowlist: optional `(module_path, allowed_bricks_imports)` map for legitimate exceptions; defaults to deny-all.

### Re-export identity

`tests/unit/lib/test_glob_reexport.py`:

```python
from nexus.utils.glob import glob_match as new
from nexus.bricks.search.primitives.glob_helpers import glob_match as old
assert new is old                      # single source of truth
```

Same shape for `oauth_factory`, `profile`, `credential_pool`, `classify_google_error`, `token_manager`.

### Existing tests

- All connector tests (`gmail`, `slack`, `gdrive`, `calendar`, `x`, `oauth_base`) must continue to pass — confirms re-exports preserve runtime semantics.
- `tests/unit/fs/test_slim_external_write.py` may need cleanup of the assertion that "non-external slim writes still surface `_kernel=None` errors" — that condition no longer exists under the new contract.

### Manual verification (PR test plan)

- `cd packages/nexus-fs && pip install -e .` in clean venv → `python -c "import nexus.fs; nexus.fs.mount_sync('local:///tmp/x')"` succeeds.
- `python -c "from nexus.backends.connectors.x.connector import PathXBackend"` succeeds in slim install.
- `python -c "from nexus.bricks.search.primitives.glob_helpers import glob_match"` succeeds in full-runtime install (back-compat).

## Risks

1. **`nexus-runtime` wheel platform coverage.** Maturin builds need `manylinux`, `macos-{x86_64,arm64}`, `windows`, `cpython-3.14`. If wheels aren't published for a target platform, `pip install nexus-fs` falls back to a source build (Rust toolchain required). Pre-merge: confirm the wheel index ships for advertised platforms; document the source-build fallback in the README.

2. **Version skew.** `nexus-fs 0.4.9` and `nexus-runtime 0.10.0` aren't pinned together today. We use `nexus-runtime>=0.10,<0.11` (compatible-release). Future bumps require coordinated releases.

3. **Re-export observability.** `inspect.getmodule(obj).__name__` returns the new path, not the old. Audit grep for `__module__` / `getmodule` over the affected symbols before merge; tests prefer `is` identity.

4. **Circular imports.** `nexus.bricks.*` re-exports from `nexus.lib.*` is safe (lib is lower-tier). `nexus.lib.*` must not pull from `nexus.bricks`. The release-integrity test enforces this for slim-shipped modules; existing bricks tests guard the full-runtime side.

5. **Integrity test scope.** `walk_packages` may surface a module that legitimately needs `nexus.bricks` (none today, but defensive). Test takes an explicit allowlist; default-deny.

6. **Single-PR review burden.** ~7 commits across packaging, lib re-homes, connector edits, bricks re-exports, tests, CI. Mitigation: clean per-commit messages let reviewers step through; the integrity test pins behavior so a regression in any commit fails CI immediately.

## Rollback

- **Whole PR:** single `git revert <merge-commit>` brings the slim package back to the current broken state. No data migration. No on-disk schema change.
- **Per-commit:** each commit is independently revertable; lib re-homes can be left in place even if the dep declaration is reverted (re-exports are harmless).

## Decision log

- Picked **B (Rust as hard dep)** over A (pure-Python re-implementation, weeks of work) and C (optional extras, doubles test matrix). Rationale: smallest diff, eliminates the silent-import-failure class.
- Chose **move-and-reexport** for #3944 over carving out `bricks/search/primitives` in the slim wheel. Rationale: keeps the existing `**/bricks/**` exclusion intact, single source of truth for shared helpers.
- **Single PR** over a 2-PR split. Rationale: the contract change and the connector sweep both land behind the new install-smoke + integrity test; splitting would let one merge without the other and leave a window where the integrity test references modules that haven't moved yet.
- Sweep all `nexus.bricks` connector imports (not only X). Rationale: the integrity test mandated by #3944's ACs would trip on the deferred imports anyway.

## Out of scope

- `nexus-ai-fs` packaging (unchanged).
- Restoring a Python-only metastore.
- Connector behavior or feature changes.
- Windows CI for slim-wheel-smoke (deferred until `nexus-runtime` ships Windows wheels and the team confirms support).
