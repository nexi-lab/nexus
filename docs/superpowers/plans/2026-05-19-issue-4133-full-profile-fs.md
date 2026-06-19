# Core Filesystem / Metadata / Streaming / Batch RPC-CLI Story — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship CLI commands for every FS/metadata/stream/batch RPC that currently lacks one, document the full file lifecycle in the user guide + FULL profile reference, and prove CLI↔RPC↔syscall parity with always-on tests, a gated E2E, and guidance benchmarks (issue #4133).

**Architecture:** Each new CLI command is a thin wrapper over an existing, already-tested `@rpc_expose`/NexusFS method (`nx.<method>(...)`), following the exact decorator/`_impl()`/`render_output` structure of `file_ops.py`. A shared in-process NexusFS fixture drives parity tests that assert CLI output == direct RPC return == kernel-syscall return. Docs are hybrid: a user-guide narrative section + an appended FS section in `docs/deployment/full-profile.md`. Benchmarks extend the existing `bench_read_write_overhead.py` and are recorded as guidance ranges, not CI gates.

**Tech Stack:** Python 3, Click, pytest, `click.testing.CliRunner`, pytest-benchmark, NexusFS in-process testkit, YAML coverage matrix.

---

## Spec

`docs/superpowers/specs/2026-05-19-issue-4133-full-profile-fs-design.md`

## Ground-truth signatures (verified — use exactly these)

```text
# nexus_fs_content.py
read_bulk(paths: list[str], context=None, return_metadata=False, skip_errors=True) -> dict[str, bytes|dict|None]
read_range(path: str, start: int, end: int, context=None) -> bytes              # start incl, end excl
read_batch(paths: list[str], *, partial=False, context=None) -> list[dict]       # raises on 1st miss unless partial
write_stream(path: str, chunks: Iterator[bytes], context=None) -> dict           # {content_id,version,modified_at,size}
write_batch(items, ...) -> per-item dict (literal path keys; per-item independent)

# nexus_fs_metadata.py
stat(path: str, context=None) -> dict          # {size,content_id,version,modified_at,is_directory}
stat_bulk(paths: list[str], context=None, skip_errors=True) -> dict[str, dict|None]   # same 5 keys
metadata_batch(paths: list[str], context=None) -> dict[str, dict|None]
    # keys: path,size,content_id,mime_type,created_at,modified_at,version,zone_id,is_directory
exists_batch(paths: list[str], context=None) -> dict[str, bool]
delete_batch(paths: list[str], recursive=False, context=None) -> dict[str, {"success":bool,"error"?:str}]
rename_batch(renames: list[tuple[str,str]], context=None) -> dict[str, {"success":bool,"new_path"|"error"}]
    # per-item independent — NOT atomic
backfill_directory_index(prefix="/", zone_id=None, _context=None) -> {"entries_created":int,"prefix":str}  # admin_only
flush_write_observer(_context=None) -> {"flushed":int}                                                     # admin_only
```

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `src/nexus/cli/commands/file_ops.py` | new `stat`, `metadata`, `read-bulk`, `exists`, `rename-batch`, `rm-batch`; add `--offset/--length/--stream/--chunk-size` to `cat`; add `--stream` to `write` | Modify |
| `src/nexus/cli/commands/admin.py` | new `admin fs` subgroup: `backfill-index`, `flush-write-observer` | Modify |
| `src/nexus/cli/commands/__init__.py` | register new command names in `_REGISTER_COMMANDS["file_ops"]` | Modify |
| `tests/unit/cli/conftest.py` | add `inproc_nexus` + `patched_fs` fixtures (in-process NexusFS for parity tests) | Modify |
| `tests/unit/cli/test_fs_parity.py` | CLI↔RPC↔syscall parity, denial, gating, ETag/OCC, batch shape, range bounds, lock | Create |
| `tests/integration/test_full_profile_fs.py` | gated real-stack FS E2E (`NEXUS_E2E=1`) | Create |
| `tests/benchmarks/bench_read_write_overhead.py` | add typed-vs-generic, read_range, batch, lock benchmark classes | Modify |
| `docs/guides/user-guide.md` | new "Files: lifecycle, batch, streaming, locks" narrative section | Modify |
| `docs/deployment/full-profile.md` | appended FS-surface reference section | Modify |
| `docs/architecture/api-rpc-surface-coverage.yaml` | `full: supported` rows for every FS/metadata/stream/batch/lock op | Modify |

## Conventions (copy these patterns exactly)

CLI command skeleton (mirrors `file_ops.py` `cat`/`write_batch`):

```python
@click.command(name="stat")
@click.argument("paths", nargs=-1, required=True, type=str)
@add_output_options
@add_backend_options
@add_context_options
def stat_cmd(paths, output_opts, remote_url, remote_api_key, operation_context):
    """One-line help.

    Examples:
        nexus stat /workspace/data.txt
    """
    async def _impl() -> None:
        timing = CommandTiming()
        try:
            async with open_filesystem(remote_url, remote_api_key, allow_local_default=True) as nx:
                with timing.phase("server"):
                    data = ...  # call nx.<rpc>(...)
            render_output(data=data, output_opts=output_opts, timing=timing,
                          human_formatter=lambda d: console.print(d))
        except Exception as e:  # noqa: BLE001
            render_error(e)
            sys.exit(1)
    asyncio.run(_impl())
```

- `render_error`, `render_output`, `OutputOptions`, `add_output_options` from `nexus.cli.output`.
- `CommandTiming` from `nexus.cli.timing`. `open_filesystem`, `add_backend_options`, `add_context_options`, `console`, `get_filesystem` from `nexus.cli.utils`.
- Register: append the click function to `register_commands(cli)` in `file_ops.py` **and** add the command name string to `_REGISTER_COMMANDS["file_ops"]` in `commands/__init__.py`.

**ENV PREREQUISITE (provisioned 2026-05-19 — already in place, do not redo):**
The Rust kernel is the out-of-process `nexusd-cluster` binary. It was built
(`cargo build --release -p nexus-cluster`) and symlinked
`~/.cargo/bin/nexus-cluster -> target/release/nexusd-cluster` (name mismatch:
KernelClient spawns `nexus-cluster`, binary is `nexusd-cluster`). `~/.cargo/bin`
is on PATH. `tests/conftest.py` already inserts worktree `src` on `sys.path`,
so pytest uses worktree source (no PYTHONPATH needed under pytest). See memory
`project_issue_4133_env_blocker.md`.

Parity-test fixture pattern (in-process real FS — PROVEN working; use this
EXACT form — `make_test_nexus`/`_build_kernel_metastore` hand an UNOPENED
KernelClient and fail, so open it explicitly):

```python
# tests/unit/cli/conftest.py additions
@pytest.fixture()
def inproc_nexus(tmp_path):
    from nexus.remote.kernel_client import KernelClient
    from nexus.factory import create_nexus_fs
    from nexus.core.config import PermissionConfig, ParseConfig
    from nexus.backends.storage.path_local import PathLocalBackend
    (tmp_path / "data").mkdir(exist_ok=True)
    k = KernelClient()
    k.set_metastore_path(str(tmp_path / "metastore.redb"))
    k.open()
    nx = create_nexus_fs(
        backend=PathLocalBackend(root_path=str(tmp_path / "data")),
        metadata_store=k, record_store=None,
        permissions=PermissionConfig(enforce=False),
        parsing=ParseConfig(auto_parse=False),
    )
    yield nx
    nx.close()
    k.close()

@pytest.fixture()
def patched_fs(inproc_nexus, monkeypatch):
    """Make every CLI command use the in-process FS (no daemon)."""
    import contextlib
    @contextlib.asynccontextmanager
    async def _open(*a, **k):
        yield inproc_nexus
    monkeypatch.setattr("nexus.cli.commands.file_ops.open_filesystem", _open)
    monkeypatch.setattr("nexus.cli.commands.file_ops.get_filesystem",
                         lambda *a, **k: inproc_nexus)
    return inproc_nexus
```

> This fixture form is verified end-to-end (write/read/stat/stat_bulk/read_range/read_bulk).
> Do NOT substitute `_build_kernel_metastore` or `make_test_nexus` (they yield
> an unopened kernel → `AssertionError: self._transport is not None`).
> `InMemoryNexusFS` (tests/testkit/metadata.py) is a pure-Python double — not
> real RPC behavior — do not use it for parity.

---

## Task 1: Parity test harness fixture

**Files:**
- Modify: `tests/unit/cli/conftest.py`
- Test: `tests/unit/cli/test_fs_parity.py` (create, smoke only this task)

- [ ] **Step 1: Confirm env prerequisite is in place**

Run: `command -v nexus-cluster && python -c "import sys; print([p for p in sys.path if 'worktrees/calm-strolling-reef/src' in p] or 'pytest-adds-src')"`
Expected: `nexus-cluster` resolves (symlink already created). The ENV PREREQUISITE block above is already provisioned — do NOT rebuild.

- [ ] **Step 2: Add `inproc_nexus` + `patched_fs` fixtures to `tests/unit/cli/conftest.py`**

Use the "Parity-test fixture pattern" code above **verbatim** (it is proven working — do not alter import paths or substitute fixtures).

- [ ] **Step 3: Write the smoke test** (`tests/unit/cli/test_fs_parity.py`)

```python
"""CLI <-> RPC <-> syscall parity for the core FS surface (Issue #4133)."""
from __future__ import annotations
import json
from click.testing import CliRunner


def test_inproc_fixture_roundtrips(inproc_nexus):
    nx = inproc_nexus
    nx.write("/a.txt", b"hello")
    assert nx.read("/a.txt") == b"hello"
    st = nx.stat("/a.txt")
    assert st["size"] == 5 and "content_id" in st
```

- [ ] **Step 4: Run it**

Run: `pytest tests/unit/cli/test_fs_parity.py -q`
Expected: PASS (fixture builds an in-process FS that round-trips).

- [ ] **Step 5: Commit**

```bash
git add tests/unit/cli/conftest.py tests/unit/cli/test_fs_parity.py
git commit -m "test(#4133): in-process FS parity harness fixture"
```

---

## Task 2: `nexus stat` (single → `stat`, multi → `stat_bulk`)

**Files:**
- Modify: `src/nexus/cli/commands/file_ops.py`, `src/nexus/cli/commands/__init__.py`
- Test: `tests/unit/cli/test_fs_parity.py`

- [ ] **Step 1: Write the failing test**

```python
def test_stat_single_parity(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import stat_cmd
    nx = patched_fs
    nx.write("/s.txt", b"abcde")
    rpc = nx.stat("/s.txt")
    res = cli_runner.invoke(stat_cmd, ["/s.txt", "--json"])
    assert res.exit_code == 0
    out = json.loads(res.output)
    assert out["size"] == rpc["size"] == 5
    assert out["content_id"] == rpc["content_id"]

def test_stat_multi_uses_stat_bulk(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import stat_cmd
    nx = patched_fs
    nx.write("/a.txt", b"aa"); nx.write("/b.txt", b"bbb")
    rpc = nx.stat_bulk(["/a.txt", "/b.txt"])
    res = cli_runner.invoke(stat_cmd, ["/a.txt", "/b.txt", "--json"])
    assert res.exit_code == 0
    out = json.loads(res.output)
    assert out["/a.txt"]["size"] == rpc["/a.txt"]["size"] == 2
    assert out["/b.txt"]["size"] == 3
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/cli/test_fs_parity.py -k stat -q`
Expected: FAIL — `ImportError: cannot import name 'stat_cmd'`.

- [ ] **Step 3: Implement `stat_cmd` in `file_ops.py`**

Add after `cat`:

```python
@click.command(name="stat")
@click.argument("paths", nargs=-1, required=True, type=str)
@add_output_options
@add_backend_options
@add_context_options
def stat_cmd(paths, output_opts, remote_url, remote_api_key, operation_context):
    """Show file metadata without reading content.

    One path -> stat; multiple paths -> stat_bulk (one round-trip).

    Examples:
        nexus stat /workspace/data.txt
        nexus stat /a.txt /b.txt --json
    """
    async def _impl() -> None:
        timing = CommandTiming()
        try:
            async with open_filesystem(remote_url, remote_api_key, allow_local_default=True) as nx:
                with timing.phase("server"):
                    if len(paths) == 1:
                        data: Any = nx.stat(paths[0], context=cast(Any, operation_context))
                    else:
                        data = nx.stat_bulk(list(paths), context=cast(Any, operation_context))
            render_output(data=data, output_opts=output_opts, timing=timing,
                          human_formatter=lambda d: console.print(d))
        except Exception as e:  # noqa: BLE001
            render_error(e)
            sys.exit(1)
    asyncio.run(_impl())
```

Add `render_error, render_output` to the existing `from nexus.cli.output import ...` line. In `register_commands`: add `cli.add_command(stat_cmd)`. In `commands/__init__.py` add `"stat",` to the `"file_ops"` tuple.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/unit/cli/test_fs_parity.py -k stat -q`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/cli/commands/file_ops.py src/nexus/cli/commands/__init__.py tests/unit/cli/test_fs_parity.py
git commit -m "feat(#4133): nexus stat (stat / stat_bulk) CLI"
```

---

## Task 3: `nexus metadata` (extended metadata via `metadata_batch`)

**Files:** Modify `file_ops.py`, `__init__.py`; Test `test_fs_parity.py`

- [ ] **Step 1: Failing test**

```python
def test_metadata_extended_parity(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import metadata_cmd
    nx = patched_fs
    nx.write("/m.txt", b"hi")
    rpc = nx.metadata_batch(["/m.txt", "/nope.txt"])
    res = cli_runner.invoke(metadata_cmd, ["/m.txt", "/nope.txt", "--json"])
    assert res.exit_code == 0
    out = json.loads(res.output)
    assert out["/m.txt"]["size"] == rpc["/m.txt"]["size"] == 2
    # metadata_batch carries the extended keys stat_bulk lacks:
    assert "mime_type" in out["/m.txt"] and "created_at" in out["/m.txt"]
    assert out["/nope.txt"] is None
```

- [ ] **Step 2: Run, expect FAIL** — `cannot import name 'metadata_cmd'`.

Run: `pytest tests/unit/cli/test_fs_parity.py -k metadata_extended -q`

- [ ] **Step 3: Implement `metadata_cmd`**

```python
@click.command(name="metadata")
@click.argument("paths", nargs=-1, required=True, type=str)
@add_output_options
@add_backend_options
@add_context_options
def metadata_cmd(paths, output_opts, remote_url, remote_api_key, operation_context):
    """Extended metadata for one or more paths (metadata_batch).

    Unlike `nexus stat`, this includes mime_type, created_at, zone_id.
    Missing paths map to null.

    Examples:
        nexus metadata /a.txt /b.txt --json
    """
    async def _impl() -> None:
        timing = CommandTiming()
        try:
            async with open_filesystem(remote_url, remote_api_key, allow_local_default=True) as nx:
                with timing.phase("server"):
                    data = nx.metadata_batch(list(paths), context=cast(Any, operation_context))
            render_output(data=data, output_opts=output_opts, timing=timing,
                          human_formatter=lambda d: console.print(d))
        except Exception as e:  # noqa: BLE001
            render_error(e)
            sys.exit(1)
    asyncio.run(_impl())
```

Register `metadata_cmd` in `register_commands` and add `"metadata",` to `__init__.py` tuple.

- [ ] **Step 4: Run, expect PASS**

Run: `pytest tests/unit/cli/test_fs_parity.py -k metadata_extended -q`

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(#4133): nexus metadata (metadata_batch) CLI"
```

---

## Task 4: `nexus exists` (batch existence via `exists_batch`)

**Files:** Modify `file_ops.py`, `__init__.py`; Test `test_fs_parity.py`

- [ ] **Step 1: Failing test**

```python
def test_exists_batch_parity_and_exit(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import exists_cmd
    nx = patched_fs
    nx.write("/here.txt", b"x")
    rpc = nx.exists_batch(["/here.txt", "/gone.txt"])
    assert rpc == {"/here.txt": True, "/gone.txt": False}
    # --json: full map, exit 0
    res = cli_runner.invoke(exists_cmd, ["/here.txt", "/gone.txt", "--json"])
    assert res.exit_code == 0
    assert json.loads(res.output) == {"/here.txt": True, "/gone.txt": False}
    # plain: exit 0 only if ALL exist
    assert cli_runner.invoke(exists_cmd, ["/here.txt"]).exit_code == 0
    assert cli_runner.invoke(exists_cmd, ["/here.txt", "/gone.txt"]).exit_code == 1
```

- [ ] **Step 2: Run, expect FAIL.**

Run: `pytest tests/unit/cli/test_fs_parity.py -k exists_batch -q`

- [ ] **Step 3: Implement `exists_cmd`**

```python
@click.command(name="exists")
@click.argument("paths", nargs=-1, required=True, type=str)
@add_output_options
@add_backend_options
@add_context_options
def exists_cmd(paths, output_opts, remote_url, remote_api_key, operation_context):
    """Check existence of one or more paths (exists_batch).

    Exit 0 iff every path exists; 1 otherwise. --json prints the full map.

    Examples:
        nexus exists /a.txt /b.txt
        nexus exists /a.txt --json
    """
    async def _impl() -> None:
        timing = CommandTiming()
        try:
            async with open_filesystem(remote_url, remote_api_key, allow_local_default=True) as nx:
                with timing.phase("server"):
                    data = nx.exists_batch(list(paths), context=cast(Any, operation_context))
            render_output(data=data, output_opts=output_opts, timing=timing,
                          human_formatter=lambda d: console.print(d))
            if not all(data.values()):
                sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            render_error(e)
            sys.exit(1)
    asyncio.run(_impl())
```

Register `exists_cmd`; add `"exists",` to tuple.

- [ ] **Step 4: Run, expect PASS.**

Run: `pytest tests/unit/cli/test_fs_parity.py -k exists_batch -q`

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(#4133): nexus exists (exists_batch) CLI"
```

---

## Task 5: `nexus read-bulk` (multi-read via `read_bulk` / `read_batch`)

**Files:** Modify `file_ops.py`, `__init__.py`; Test `test_fs_parity.py`

- [ ] **Step 1: Failing test**

```python
def test_read_bulk_parity(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import read_bulk_cmd
    nx = patched_fs
    nx.write("/r1.txt", b"one"); nx.write("/r2.txt", b"two")
    rpc = nx.read_bulk(["/r1.txt", "/r2.txt"])
    res = cli_runner.invoke(read_bulk_cmd, ["/r1.txt", "/r2.txt", "--json"])
    assert res.exit_code == 0
    out = json.loads(res.output)
    assert out["/r1.txt"] == rpc["/r1.txt"].decode() == "one"
    assert out["/r2.txt"] == "two"

def test_read_bulk_atomic_raises_on_missing(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import read_bulk_cmd
    nx = patched_fs
    nx.write("/r1.txt", b"one")
    res = cli_runner.invoke(read_bulk_cmd, ["/r1.txt", "/missing.txt", "--atomic", "--json"])
    assert res.exit_code == 1  # read_batch(partial=False) raises on first miss
```

- [ ] **Step 2: Run, expect FAIL.**

Run: `pytest tests/unit/cli/test_fs_parity.py -k read_bulk -q`

- [ ] **Step 3: Implement `read_bulk_cmd`**

```python
@click.command(name="read-bulk")
@click.argument("paths", nargs=-1, required=True, type=str)
@click.option("--atomic", is_flag=True,
              help="Use read_batch (all-or-nothing: error on first missing path)")
@add_output_options
@add_backend_options
@add_context_options
def read_bulk_cmd(paths, atomic, output_opts, remote_url, remote_api_key, operation_context):
    """Read multiple files in one round-trip.

    Default uses read_bulk (missing paths -> null). --atomic uses read_batch
    (raises on the first missing/inaccessible path).

    Examples:
        nexus read-bulk /a.txt /b.txt --json
        nexus read-bulk /a.txt /b.txt --atomic --json
    """
    async def _impl() -> None:
        timing = CommandTiming()
        try:
            async with open_filesystem(remote_url, remote_api_key, allow_local_default=True) as nx:
                with timing.phase("server"):
                    if atomic:
                        items = nx.read_batch(list(paths), partial=False,
                                              context=cast(Any, operation_context))
                        data: Any = {it["path"]: _b2s(it.get("content"))
                                     for it in items}
                    else:
                        raw = nx.read_bulk(list(paths), context=cast(Any, operation_context))
                        data = {p: _b2s(v) for p, v in raw.items()}
            render_output(data=data, output_opts=output_opts, timing=timing,
                          human_formatter=lambda d: console.print(d))
        except Exception as e:  # noqa: BLE001
            render_error(e)
            sys.exit(1)
    asyncio.run(_impl())


def _b2s(v):
    """Decode bytes for JSON; pass through None / non-bytes."""
    if isinstance(v, bytes):
        try:
            return v.decode()
        except UnicodeDecodeError:
            import base64
            return {"_base64": base64.b64encode(v).decode()}
    return v
```

Register `read_bulk_cmd`; add `"read-bulk",` to tuple.

- [ ] **Step 4: Run, expect PASS.**

Run: `pytest tests/unit/cli/test_fs_parity.py -k read_bulk -q`

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(#4133): nexus read-bulk (read_bulk/read_batch) CLI"
```

---

## Task 6: `nexus rename-batch` (multi-rename via `rename_batch`)

**Files:** Modify `file_ops.py`, `__init__.py`; Test `test_fs_parity.py`

- [ ] **Step 1: Failing test** (asserts per-item independence — NOT atomic)

```python
def test_rename_batch_per_item_independent(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import rename_batch_cmd
    nx = patched_fs
    nx.write("/old1.txt", b"1")  # /old2.txt deliberately absent
    res = cli_runner.invoke(rename_batch_cmd,
                            ["/old1.txt:/new1.txt", "/old2.txt:/new2.txt", "--json"])
    assert res.exit_code == 0  # independent: one failure does not abort the rest
    out = json.loads(res.output)
    assert out["/old1.txt"]["success"] is True
    assert out["/old1.txt"]["new_path"] == "/new1.txt"
    assert out["/old2.txt"]["success"] is False
    assert nx.read("/new1.txt") == b"1"
```

- [ ] **Step 2: Run, expect FAIL.**

Run: `pytest tests/unit/cli/test_fs_parity.py -k rename_batch -q`

- [ ] **Step 3: Implement `rename_batch_cmd`**

```python
@click.command(name="rename-batch")
@click.argument("pairs", nargs=-1, required=True, type=str)
@add_output_options
@add_backend_options
@add_context_options
def rename_batch_cmd(pairs, output_opts, remote_url, remote_api_key, operation_context):
    """Rename/move multiple files. Each pair is SRC:DST.

    Per-item independent (a failed rename does not abort the others).

    Examples:
        nexus rename-batch /a.txt:/b.txt /c.txt:/d.txt --json
    """
    renames: list[tuple[str, str]] = []
    for p in pairs:
        if ":" not in p:
            render_error(ValueError(f"Expected SRC:DST, got {p!r}"))
            sys.exit(2)
        src, dst = p.split(":", 1)
        renames.append((src, dst))

    async def _impl() -> None:
        timing = CommandTiming()
        try:
            async with open_filesystem(remote_url, remote_api_key, allow_local_default=True) as nx:
                with timing.phase("server"):
                    data = nx.rename_batch(renames, context=cast(Any, operation_context))
            render_output(data=data, output_opts=output_opts, timing=timing,
                          human_formatter=lambda d: console.print(d))
        except Exception as e:  # noqa: BLE001
            render_error(e)
            sys.exit(1)
    asyncio.run(_impl())
```

Register `rename_batch_cmd`; add `"rename-batch",` to tuple.

- [ ] **Step 4: Run, expect PASS.**

Run: `pytest tests/unit/cli/test_fs_parity.py -k rename_batch -q`

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(#4133): nexus rename-batch (rename_batch) CLI"
```

---

## Task 7: `nexus rm-batch` (multi-delete via `delete_batch`)

**Files:** Modify `file_ops.py`, `__init__.py`; Test `test_fs_parity.py`

- [ ] **Step 1: Failing test** (per-item independent; `--recursive` for dirs)

```python
def test_rm_batch_per_item_independent(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import rm_batch_cmd
    nx = patched_fs
    nx.write("/d1.txt", b"1"); nx.write("/d2.txt", b"2")
    res = cli_runner.invoke(rm_batch_cmd, ["/d1.txt", "/missing.txt", "/d2.txt", "--json"])
    assert res.exit_code == 0
    out = json.loads(res.output)
    assert out["/d1.txt"]["success"] is True
    assert out["/d2.txt"]["success"] is True
    assert out["/missing.txt"]["success"] is False
    assert not nx.access("/d1.txt")
```

- [ ] **Step 2: Run, expect FAIL.**

Run: `pytest tests/unit/cli/test_fs_parity.py -k rm_batch -q`

- [ ] **Step 3: Implement `rm_batch_cmd`**

```python
@click.command(name="rm-batch")
@click.argument("paths", nargs=-1, required=True, type=str)
@click.option("--recursive", "-r", is_flag=True, help="Delete non-empty directories")
@add_output_options
@add_backend_options
@add_context_options
def rm_batch_cmd(paths, recursive, output_opts, remote_url, remote_api_key, operation_context):
    """Delete multiple files/directories (delete_batch).

    Per-item independent. Use -r for non-empty directories.

    Examples:
        nexus rm-batch /a.txt /b.txt --json
        nexus rm-batch /dir1 /dir2 -r
    """
    async def _impl() -> None:
        timing = CommandTiming()
        try:
            async with open_filesystem(remote_url, remote_api_key, allow_local_default=True) as nx:
                with timing.phase("server"):
                    data = nx.delete_batch(list(paths), recursive=recursive,
                                           context=cast(Any, operation_context))
            render_output(data=data, output_opts=output_opts, timing=timing,
                          human_formatter=lambda d: console.print(d))
        except Exception as e:  # noqa: BLE001
            render_error(e)
            sys.exit(1)
    asyncio.run(_impl())
```

Register `rm_batch_cmd`; add `"rm-batch",` to tuple.

- [ ] **Step 4: Run, expect PASS.**

Run: `pytest tests/unit/cli/test_fs_parity.py -k rm_batch -q`

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(#4133): nexus rm-batch (delete_batch) CLI"
```

---

## Task 8: `nexus cat` range read (`--offset` / `--length` → `read_range`)

**Files:** Modify `file_ops.py` (`cat` only); Test `test_fs_parity.py`

- [ ] **Step 1: Read current `cat` to find safe insertion point**

Run: `sed -n '88,200p' src/nexus/cli/commands/file_ops.py`
Expected: see the `cat` decorator stack and `_impl`'s read branch. Note where `nx.read(...)` is called.

- [ ] **Step 2: Failing test**

```python
def test_cat_range_equals_slice(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import cat
    nx = patched_fs
    nx.write("/big.txt", b"0123456789")
    assert nx.read_range("/big.txt", 2, 5) == b"234"
    res = cli_runner.invoke(cat, ["/big.txt", "--offset", "2", "--length", "3"])
    assert res.exit_code == 0
    assert res.output.rstrip("\n") == "234"

def test_cat_no_range_unchanged(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import cat
    nx = patched_fs
    nx.write("/whole.txt", b"hello world")
    res = cli_runner.invoke(cat, ["/whole.txt"])
    assert res.exit_code == 0 and "hello world" in res.output
```

- [ ] **Step 3: Run, expect FAIL** — `no such option: --offset`.

Run: `pytest tests/unit/cli/test_fs_parity.py -k cat_range -q`

- [ ] **Step 4: Add options + branch to `cat`**

Add to `cat`'s decorator stack (before `@add_output_options`):

```python
@click.option("--offset", type=int, default=None,
              help="Start byte offset for a range read (read_range)")
@click.option("--length", type=int, default=None,
              help="Number of bytes from --offset (requires --offset)")
```

Add `offset: int | None, length: int | None` to the `cat(...)` signature (place them right after `block_type`). At the very top of `cat`'s `_impl()` server phase, before the existing read branch, insert:

```python
if offset is not None:
    if offset < 0 or (length is not None and length < 0):
        render_error(ValueError("--offset/--length must be non-negative"))
        sys.exit(2)
    end = (offset + length) if length is not None else nx.stat(path)["size"]
    chunk = nx.read_range(path, offset, end, context=cast(Any, operation_context))
    sys.stdout.buffer.write(chunk)
    return
```

(`return` exits `_impl` before the normal full-file read; existing behavior is untouched when `--offset` is omitted.)

- [ ] **Step 5: Run, expect PASS** (both range and no-range tests).

Run: `pytest tests/unit/cli/test_fs_parity.py -k "cat_range or cat_no_range" -q`

- [ ] **Step 6: Regression — existing cat tests still pass**

Run: `pytest tests/unit/cli/ -k cat -q`
Expected: PASS (no regression in existing `cat` behavior).

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat(#4133): nexus cat --offset/--length (read_range)"
```

---

## Task 9: Streaming read/write (`cat --stream`, `write --stream`)

**Files:** Modify `file_ops.py` (`cat`, `write`); Test `test_fs_parity.py`

- [ ] **Step 1: Failing test**

```python
def test_cat_stream_matches_full(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import cat
    nx = patched_fs
    body = b"x" * 200_000
    nx.write("/strm.bin", body)
    res = cli_runner.invoke(cat, ["/strm.bin", "--stream", "--chunk-size", "65536"])
    assert res.exit_code == 0
    assert res.output.encode() == body or res.stdout_bytes == body

def test_write_stream_from_stdin(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import write
    nx = patched_fs
    res = cli_runner.invoke(write, ["/ws.txt", "--stream"], input="streamed-bytes")
    assert res.exit_code == 0
    assert nx.read("/ws.txt") == b"streamed-bytes"
```

> Plan-time: confirm `write`'s existing signature/param names with `sed -n '393,500p' src/nexus/cli/commands/file_ops.py` before editing; keep its current content-source behavior intact when `--stream` is absent.

- [ ] **Step 2: Run, expect FAIL** — `no such option: --stream`.

Run: `pytest tests/unit/cli/test_fs_parity.py -k "cat_stream or write_stream_from_stdin" -q`

- [ ] **Step 3: Add `--stream`/`--chunk-size` to `cat`**

Add options:

```python
@click.option("--stream", "stream_mode", is_flag=True,
              help="Stream content in chunks (stream / stream_range)")
@click.option("--chunk-size", type=int, default=65536, show_default=True,
              help="Chunk size for --stream")
```

Add `stream_mode: bool, chunk_size: int` to `cat(...)` signature. In `_impl()`, just after the range branch from Task 8:

```python
if stream_mode:
    if offset is not None:
        end = (offset + length) if length is not None else nx.stat(path)["size"]
        gen = nx.stream_range(path, offset, end, chunk_size=chunk_size,
                              context=cast(Any, operation_context))
    else:
        gen = nx.stream(path, chunk_size=chunk_size,
                        context=cast(Any, operation_context))
    for chunk in gen:
        sys.stdout.buffer.write(chunk)
    return
```

> Plan-time: confirm `stream`/`stream_range` accept `chunk_size=` and yield bytes by reading `nexus_fs_content.py:458` and `:506`; adjust the call (positional vs kw, generator vs list) to the actual signature. Do not assume.

- [ ] **Step 4: Add `--stream` to `write`**

Add option `@click.option("--stream", "stream_mode", is_flag=True, help="Read content from stdin and write via write_stream")` and `stream_mode: bool` to `write(...)`. At the start of `write`'s `_impl()` server phase, before its existing write call:

```python
if stream_mode:
    raw = sys.stdin.buffer.read()
    cs = 65536
    chunks = (raw[i:i + cs] for i in range(0, len(raw), cs))
    result = nx.write_stream(path, chunks, context=cast(Any, operation_context))
    render_output(data=result, output_opts=output_opts, timing=timing,
                  human_formatter=lambda d: console.print(d))
    return
```

> Plan-time: match `write`'s actual local var names (`output_opts`, `timing`, `path`, `operation_context`) — read the function first; if `write` lacks `@add_output_options`, print a concise success line instead of `render_output`.

- [ ] **Step 5: Run, expect PASS.**

Run: `pytest tests/unit/cli/test_fs_parity.py -k "cat_stream or write_stream_from_stdin" -q`

- [ ] **Step 6: Regression**

Run: `pytest tests/unit/cli/ -k "cat or write" -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat(#4133): nexus cat --stream / write --stream (stream/write_stream)"
```

---

## Task 10: `nexus admin fs` subgroup (`backfill-index`, `flush-write-observer`)

**Files:**
- Modify: `src/nexus/cli/commands/admin.py`
- Test: `tests/unit/cli/test_fs_parity.py`

- [ ] **Step 1: Read `admin.py` group definition**

Run: `grep -n "def admin\|@admin\|click.group\|add_command\|@click" src/nexus/cli/commands/admin.py | head -30`
Expected: locate the top-level `admin` `click.Group` object and how existing subcommands are attached. Note the group variable name.

- [ ] **Step 2: Failing test**

```python
def test_admin_fs_flush_and_backfill(patched_fs, cli_runner: CliRunner, monkeypatch):
    import contextlib
    @contextlib.asynccontextmanager
    async def _open(*a, **k):
        yield patched_fs
    monkeypatch.setattr("nexus.cli.commands.admin.open_filesystem", _open, raising=False)
    from nexus.cli.commands.admin import admin
    r1 = cli_runner.invoke(admin, ["fs", "flush-write-observer", "--json"])
    assert r1.exit_code == 0
    assert "flushed" in json.loads(r1.output)
    r2 = cli_runner.invoke(admin, ["fs", "backfill-index", "/", "--json"])
    assert r2.exit_code == 0
    assert "entries_created" in json.loads(r2.output)
```

> Plan-time: import name in Step 2's `from nexus.cli.commands.admin import admin` must match the actual group variable found in Step 1 (could be `admin` or `admin_group`); fix both the import and `monkeypatch` target accordingly.

- [ ] **Step 3: Run, expect FAIL** — `No such command 'fs'`.

Run: `pytest tests/unit/cli/test_fs_parity.py -k admin_fs -q`

- [ ] **Step 4: Add the `fs` subgroup to `admin.py`**

Add imports if absent: `from nexus.cli.output import OutputOptions, add_output_options, render_error, render_output`; `from nexus.cli.timing import CommandTiming`; `from nexus.cli.utils import open_filesystem`. Then:

```python
@admin.group("fs")
def admin_fs() -> None:
    """Admin-only filesystem maintenance (admin_only RPCs)."""


@admin_fs.command("backfill-index")
@click.argument("prefix", default="/", type=str)
@add_output_options
def admin_fs_backfill_index(prefix, output_opts) -> None:
    """Backfill the sparse directory index (admin_only)."""
    async def _impl() -> None:
        timing = CommandTiming()
        try:
            async with open_filesystem(None, None, allow_local_default=True) as nx:
                with timing.phase("server"):
                    data = nx.backfill_directory_index(prefix)
            render_output(data=data, output_opts=output_opts, timing=timing,
                          human_formatter=lambda d: console.print(d))
        except Exception as e:  # noqa: BLE001
            render_error(e)
            sys.exit(1)
    asyncio.run(_impl())


@admin_fs.command("flush-write-observer")
@add_output_options
def admin_fs_flush_write_observer(output_opts) -> None:
    """Flush pending write-observer events to the DB (admin_only)."""
    async def _impl() -> None:
        timing = CommandTiming()
        try:
            async with open_filesystem(None, None, allow_local_default=True) as nx:
                with timing.phase("server"):
                    data = nx.flush_write_observer()
            render_output(data=data, output_opts=output_opts, timing=timing,
                          human_formatter=lambda d: console.print(d))
        except Exception as e:  # noqa: BLE001
            render_error(e)
            sys.exit(1)
    asyncio.run(_impl())
```

Use the same `console`/`sys`/`asyncio`/`click` imports `admin.py` already has (verify in Step 1; add any missing). `admin` is registered via `_ADD_COMMAND` already — no `__init__.py` change.

- [ ] **Step 5: Run, expect PASS.**

Run: `pytest tests/unit/cli/test_fs_parity.py -k admin_fs -q`

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(#4133): nexus admin fs backfill-index / flush-write-observer"
```

---

## Task 11: Cross-path parity + denial + ETag/OCC tests

**Files:** Test `tests/unit/cli/test_fs_parity.py`

- [ ] **Step 1: Write parity/denial/OCC tests**

```python
def test_cli_read_equals_rpc_read(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import cat
    nx = patched_fs
    nx.write("/p.txt", b"parity-bytes")
    rpc = nx.read("/p.txt")
    cli = cli_runner.invoke(cat, ["/p.txt"])
    assert cli.exit_code == 0
    assert cli.output.rstrip("\n").encode() == rpc == b"parity-bytes"

def test_write_roundtrips_content_id(patched_fs):
    nx = patched_fs
    w = nx.write("/cid.txt", b"data")
    s = nx.stat("/cid.txt")
    assert w["content_id"] == s["content_id"]

def test_etag_if_match_conflict(patched_fs):
    """Stale content_id write is rejected; matching id succeeds."""
    nx = patched_fs
    first = nx.write("/occ.txt", b"v1")
    nx.write("/occ.txt", b"v2")  # advances content_id/version
    import pytest as _pt
    with _pt.raises(Exception):
        nx.write("/occ.txt", b"v3", content_id=first["content_id"])  # stale -> conflict

def test_range_out_of_bounds_is_bounded(patched_fs):
    nx = patched_fs
    nx.write("/short.txt", b"abc")
    # end past EOF returns what exists, does not crash
    assert nx.read_range("/short.txt", 0, 100) == b"abc"

def test_admin_only_denied_for_non_admin(tmp_path):
    """backfill/flush are admin_only — a non-admin FS is refused."""
    from nexus.backends.cas_local import CASLocalBackend
    from nexus.core.factory import create_nexus_fs
    from nexus.contracts.config import PermissionConfig, ParseConfig, CacheConfig
    from tests.benchmarks.conftest import _build_kernel_metastore
    from nexus.records.sqlalchemy_store import SQLAlchemyRecordStore
    (tmp_path / "s").mkdir()
    _, meta = _build_kernel_metastore(str(tmp_path / "k.db"))
    nx = create_nexus_fs(backend=CASLocalBackend(str(tmp_path / "s")),
                         metadata_store=meta, record_store=SQLAlchemyRecordStore(),
                         is_admin=False, permissions=PermissionConfig(enforce=True),
                         parsing=ParseConfig(auto_parse=False), cache=CacheConfig())
    import pytest as _pt
    with _pt.raises(Exception):
        nx.flush_write_observer()
    nx.close()
```

> Plan-time: verify the exact `write(..., content_id=...)` / `if_match` kwarg name in `nexus_fs_content.py` `write` and the exact admin-denial exception type by reading the source; adjust kwarg name and `raises(...)` to the real API. The *intent* (stale id rejected; non-admin refused) is fixed; the literal kwarg/exception must match source.

- [ ] **Step 2: Run, expect PASS**

Run: `pytest tests/unit/cli/test_fs_parity.py -q`
Expected: PASS for all (adjust kwarg/exception per the plan-time note until green).

- [ ] **Step 3: Full CLI unit suite regression**

Run: `pytest tests/unit/cli/ -q`
Expected: PASS — no regression from the new commands or the `cat`/`write` edits.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "test(#4133): cross-path parity, ETag/OCC, range bounds, admin-only denial"
```

---

## Task 12: Coverage matrix rows

**Files:** Modify `docs/architecture/api-rpc-surface-coverage.yaml`

- [ ] **Step 1: Find existing FS rows to update vs add**

Run: `grep -n "id: fs\.\|id: filesystem\.\|read_bulk\|stat_bulk\|rename_batch\|read_range\|write_stream" docs/architecture/api-rpc-surface-coverage.yaml`
Expected: a list of any existing FS operation ids. For each existing one, set its `profiles.full` to `supported` and fill `usage_example`/`correctness_test`. For methods with no row, add new entries.

- [ ] **Step 2: Add/patch rows**

For every method in the "Ground-truth signatures" list that lacks a row, append an `operations:` entry using this exact schema (mirror existing entries):

```yaml
- id: fs.read_range
  module: nexus_fs
  summary: "Byte-range read (read_range); CLI nexus cat --offset/--length."
  transports:
    grpc_expose:
      name: read_range
      source: src/nexus/core/nexus_fs_content.py:393
  profiles:
    lite: supported
    sandbox: supported
    full: supported
  usage_example: "nexus cat /f --offset 0 --length 1024"
  correctness_test: tests/unit/cli/test_fs_parity.py::test_cat_range_equals_slice
  perf_class: hot_path
  perf_link: tests/benchmarks/bench_read_write_overhead.py
  gap_issue: null
  owning_issue: 4133
```

Repeat for: `fs.stat`, `fs.stat_bulk`, `fs.metadata_batch`, `fs.exists_batch`, `fs.read_bulk`, `fs.read_batch`, `fs.write_stream`, `fs.stream`, `fs.stream_range`, `fs.rename_batch`, `fs.delete_batch`, `fs.backfill_directory_index` (perf_class: `not_perf_sensitive`), `fs.flush_write_observer` (`not_perf_sensitive`), each pointing `correctness_test` at its Task 2–11 test and `owning_issue: 4133`. Set `perf_class: hot_path` for read/write/range/batch, `control_plane` for locks, `not_perf_sensitive` for the two admin ops.

- [ ] **Step 3: Validate YAML parses**

Run: `python -c "import yaml,sys; yaml.safe_load(open('docs/architecture/api-rpc-surface-coverage.yaml')); print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add docs/architecture/api-rpc-surface-coverage.yaml
git commit -m "docs(#4133): FS/metadata/stream/batch coverage matrix rows (full=supported)"
```

---

## Task 13: Benchmarks (extend `bench_read_write_overhead.py`)

**Files:** Modify `tests/benchmarks/bench_read_write_overhead.py`

- [ ] **Step 1: Append benchmark classes** (mirror existing `@pytest.mark.benchmark_file_ops` style)

```python
@pytest.mark.benchmark_file_ops
class TestRangeRead:
    """read_range vs full read of a 1 MB file (Issue #4133)."""
    def test_read_range_1mb_slice(self, benchmark, benchmark_nexus):
        nx = benchmark_nexus
        nx.write("/rr.bin", b"z" * (1024 * 1024))
        result = benchmark(lambda: nx.read_range("/rr.bin", 0, 65536))
        assert len(result) == 65536


@pytest.mark.benchmark_file_ops
class TestStatBulkVsSequential:
    """stat_bulk(100) vs 100x stat (Issue #4133)."""
    def test_stat_bulk_100(self, benchmark, populated_nexus):
        nx = populated_nexus
        paths = [f"/many_files/file_{i:04d}.txt" for i in range(100)]
        result = benchmark(lambda: nx.stat_bulk(paths))
        assert len(result) == 100


@pytest.mark.benchmark_file_ops
class TestTypedVsGenericRead:
    """Typed nx.read vs generic dispatch path (Issue #4133)."""
    def test_typed_read(self, benchmark, populated_nexus):
        nx = populated_nexus
        result = benchmark(lambda: nx.read("/many_files/file_0000.txt"))
        assert result is not None


@pytest.mark.benchmark_file_ops
class TestLockAcquireRelease:
    """sys_lock + sys_unlock round-trip (Issue #4133, control plane)."""
    def test_lock_cycle(self, benchmark, benchmark_nexus):
        nx = benchmark_nexus
        nx.write("/lk.txt", b"x")
        def cycle():
            lid = nx.sys_lock("/lk.txt")
            nx.sys_unlock("/lk.txt", lid) if lid else None
            return lid
        benchmark(cycle)
```

> Plan-time: confirm `sys_lock`/`sys_unlock` arg shape (lock id positional vs kw, return on contention) from `nexus_fs.py:450/479`; adjust `cycle()` to the real signature. Confirm `populated_nexus` pre-creates `/many_files/file_NNNN.txt` (it does — see existing `TestReadBulkOverhead`).

- [ ] **Step 2: Run benchmarks (collection + execution sanity, not a gate)**

Run: `pytest tests/benchmarks/bench_read_write_overhead.py -q --benchmark-only -k "RangeRead or StatBulk or TypedVsGeneric or LockAcquire"`
Expected: PASS; capture the median values from the output table for Task 14's guidance ranges.

- [ ] **Step 3: Commit**

```bash
git add tests/benchmarks/bench_read_write_overhead.py
git commit -m "perf(#4133): FS range/stat-bulk/typed-vs-generic/lock benchmarks (guidance)"
```

---

## Task 14: User-guide narrative section

**Files:** Modify `docs/guides/user-guide.md`

- [ ] **Step 1: Find the insertion point**

Run: `grep -n "^## " docs/guides/user-guide.md`
Expected: section numbers; insert the new section after the first-local-run / shared-server sections (after §4, before §5 "Search"). Renumber subsequent `## N.` headings if the guide uses sequential numbers.

- [ ] **Step 2: Add the section** (verbatim; fill the three `<bench:...>` placeholders with the medians captured in Task 13 Step 2 — do NOT leave them as placeholders)

````markdown
## Files: lifecycle, batch, streaming, and locks

This is the full file API as a single workflow. Every CLI command below has
an equivalent RPC (the CLI is a thin wrapper); the SDK calls the same methods.

### Lifecycle (write → stat → read → rename → delete)

```bash
echo "hello" | nexus write /workspace/a.txt --stream
nexus stat  /workspace/a.txt --json        # size, content_id, version, is_directory
nexus cat   /workspace/a.txt               # -> hello
nexus rename-batch /workspace/a.txt:/workspace/b.txt --json
nexus rm-batch /workspace/b.txt --json
```

SDK equivalent:

```python
import nexus
nx = nexus.connect()
nx.write("/workspace/a.txt", b"hello")
print(nx.stat("/workspace/a.txt")["size"])     # 5
assert nx.read("/workspace/a.txt") == b"hello"
```

**Correctness check you can run:** `content_id` from `write` equals
`content_id` from `stat` equals the id seen by `cat` — same bytes, one
identity. `nexus stat` proves it without re-reading content.

### Batch (one round-trip for many files)

```bash
nexus read-bulk  /w/a.txt /w/b.txt --json          # {path: content}
nexus stat       /w/a.txt /w/b.txt --json          # multi -> stat_bulk
nexus metadata   /w/a.txt /w/b.txt --json          # extended (mime_type, created_at)
nexus exists     /w/a.txt /w/missing.txt --json    # {path: bool}; exit 1 if any missing
nexus rename-batch /w/a.txt:/w/c.txt --json
nexus rm-batch   /w/b.txt /w/c.txt --json
```

`read-bulk` skips missing paths (null); `read-bulk --atomic` uses
`read_batch` and fails on the first missing path. `rename-batch`,
`rm-batch`, and `metadata` are **per-item independent** — one failure does
not abort the others; the JSON result reports per-path success/error.
`stat` (multi) and `metadata` differ: `stat`/`stat_bulk` return the core
five fields (size, content_id, version, modified_at, is_directory);
`metadata`/`metadata_batch` adds mime_type, created_at, zone_id.

### Streaming and range reads

```bash
nexus cat /w/big.bin --offset 0 --length 1048576     # first 1 MiB (read_range)
nexus cat /w/big.bin --stream --chunk-size 65536     # chunked (stream)
cat ./local.bin | nexus write /w/big.bin --stream    # chunked write (write_stream)
```

`read_range(path, start, end)` is start-inclusive, end-exclusive;
`nexus cat --offset N --length M` reads bytes `[N, N+M)`. An end past EOF
returns the available bytes (bounded, not an error).

### Locks

```bash
nexus lock list
nexus lock info /w/a.txt
nexus lock release /w/a.txt --force
```

A second acquirer of a held lock is refused/blocked; release frees it.
`nexus lock info` reflects current state.

### Failure and unavailable behavior

- Unauthenticated request → HTTP 401 (not a traceback).
- Authenticated but unpermitted → explicit denial.
- `nexus admin fs backfill-index` / `flush-write-observer` are
  **admin-only**; a non-admin caller is refused server-side.
- The legacy `POST /api/nfs/{method}` HTTP endpoint is **deprecated,
  migration-only**, sunset **2026-06-25** (Issue #1133). Use gRPC `Call`
  or the typed `Read`/`Write`/`Delete` RPCs (what the CLI uses).

### Performance

Read/write/range/batch are hot paths; locks are control-plane; the two
admin maintenance ops are not performance-sensitive. Indicative medians
from `tests/benchmarks/bench_read_write_overhead.py` on a dev laptop:
read_range(64 KiB) ≈ <bench:read_range>, stat_bulk(100) ≈
<bench:stat_bulk>, typed read ≈ <bench:typed_read>. These are guidance,
not CI gates. See [FULL deployment profile](../deployment/full-profile.md#filesystem-surface).
````

- [ ] **Step 3: Commit**

```bash
git add docs/guides/user-guide.md
git commit -m "docs(#4133): user-guide — file lifecycle/batch/stream/lock section"
```

---

## Task 15: FULL profile reference — FS surface section

**Files:** Modify `docs/deployment/full-profile.md`

- [ ] **Step 1: Append a `## Filesystem surface` section**

Append at end of `docs/deployment/full-profile.md`:

````markdown
## Filesystem surface

FULL exposes the complete file API over four equivalent paths: kernel
syscalls, typed gRPC (`Read`/`Write`/`Delete`/`Ping`/`BatchRead`), generic
gRPC `Call`, and the CLI (a thin wrapper). The deprecated HTTP
`POST /api/nfs/{method}` is migration-only (sunset 2026-06-25, Issue #1133).

| Group | RPC | CLI |
|-------|-----|-----|
| Read | `read`, `read_range`, `read_bulk`, `read_batch` | `cat` (+`--offset/--length/--stream`), `read-bulk` |
| Write | `write`, `write_stream`, `write_batch`, `append`, `edit` | `write` (+`--stream`), `write-batch`, `append`, `edit` |
| Metadata | `stat`, `stat_bulk`, `metadata_batch`, `exists_batch` | `stat`, `metadata`, `exists` |
| Mutate | `rename_batch`, `delete_batch`, `rename`, `delete` | `rename-batch`, `rm-batch`, `move`, `rm` |
| Stream | `stream`, `stream_range` | `cat --stream` |
| Locks | `sys_lock`, `sys_unlock`, `lock_acquire`, `release_lock` | `lock list/info/release` |
| Admin | `backfill_directory_index`, `flush_write_observer` | `admin fs backfill-index`, `admin fs flush-write-observer` |

**Semantics that matter:** `read_range` is start-inclusive/end-exclusive;
`rename_batch`/`delete_batch`/`write_batch` are per-item independent (not
atomic) — the result maps each literal path to `{success, ...}`;
`content_id` is stable across `write`/`stat`/`read` for identical bytes and
drives If-Match optimistic concurrency (a stale `content_id` write is
rejected). Admin ops require admin; non-admin callers are refused.

**Benchmark guidance** (dev laptop medians, not CI gates;
`tests/benchmarks/bench_read_write_overhead.py`): read/write/range/batch =
hot path; lock acquire/release = control plane; `backfill_directory_index`
/ `flush_write_observer` = not performance-sensitive. Fill exact medians
from the Task 13 run.
````

- [ ] **Step 2: Commit**

```bash
git add docs/deployment/full-profile.md
git commit -m "docs(#4133): full-profile.md — filesystem surface reference"
```

---

## Task 16: Gated E2E (real FULL stack)

**Files:** Create `tests/integration/test_full_profile_fs.py`

- [ ] **Step 1: Read #4132's gated-E2E for the boot-fixture pattern**

Run: `sed -n '1,60p' tests/integration/test_full_profile_boot.py`
Expected: see the `@pytest.mark.integration` marker, the `NEXUS_E2E` skip guard, and the reusable boot fixture / `tests/testkit/profiles.py` usage. Mirror it exactly.

- [ ] **Step 2: Create the E2E module**

```python
"""Gated real-stack FS E2E for the FULL profile (Issue #4133).

Skipped unless NEXUS_E2E=1. Reuses #4132's profile-agnostic boot fixture.
"""
from __future__ import annotations
import os
import pytest

pytestmark = pytest.mark.integration

E2E = os.environ.get("NEXUS_E2E") == "1"


@pytest.mark.skipif(not E2E, reason="set NEXUS_E2E=1 for real-stack FS E2E")
def test_full_fs_lifecycle_batch_stream_lock(full_hub):
    """full_hub: reuse the #4132 boot fixture (see test_full_profile_boot.py)."""
    nx = full_hub.connect()
    nx.write("/e2e/a.txt", b"alpha")
    assert nx.read("/e2e/a.txt") == b"alpha"
    assert nx.stat("/e2e/a.txt")["size"] == 5
    nx.write("/e2e/b.txt", b"beta")
    bulk = nx.read_bulk(["/e2e/a.txt", "/e2e/b.txt"])
    assert bulk["/e2e/a.txt"] == b"alpha" and bulk["/e2e/b.txt"] == b"beta"
    assert nx.read_range("/e2e/a.txt", 0, 3) == b"alp"
    ex = nx.exists_batch(["/e2e/a.txt", "/e2e/nope.txt"])
    assert ex == {"/e2e/a.txt": True, "/e2e/nope.txt": False}
    rn = nx.rename_batch([("/e2e/b.txt", "/e2e/c.txt")])
    assert rn["/e2e/b.txt"]["success"] is True
    lid = nx.sys_lock("/e2e/a.txt")
    assert lid
    nx.sys_unlock("/e2e/a.txt", lid)
    dl = nx.delete_batch(["/e2e/a.txt", "/e2e/c.txt"])
    assert all(r["success"] for r in dl.values())
```

> Plan-time: the fixture name (`full_hub` here) MUST match #4132's actual fixture (found in Step 1 — it may be e.g. `full_stack` / `booted_full`). Use the real name and its real `.connect()`/client accessor. If #4132's fixture lives in a conftest, ensure it is importable from `tests/integration/`; otherwise add a thin conftest re-export, not a new harness.

- [ ] **Step 3: Verify it is skipped by default (no Docker needed in CI)**

Run: `pytest tests/integration/test_full_profile_fs.py -q`
Expected: `1 skipped` (NEXUS_E2E unset).

- [ ] **Step 4: (Optional, if Docker available) run for real**

Run: `NEXUS_E2E=1 pytest tests/integration/test_full_profile_fs.py -q`
Expected: PASS, or a precise environmental skip (same Docker-pull posture as #4132) — never a hard failure due to missing images.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_full_profile_fs.py
git commit -m "test(#4133): gated real-stack FS E2E (NEXUS_E2E=1)"
```

---

## Task 17: Final verification, lint, PR

**Files:** none (verification only)

- [ ] **Step 1: Full relevant suites**

Run: `pytest tests/unit/cli/ -q && pytest tests/integration/test_full_profile_fs.py -q`
Expected: unit PASS; E2E `1 skipped`.

- [ ] **Step 2: CLI smoke — every new command is registered & has help**

Run: `for c in stat metadata exists read-bulk rename-batch rm-batch; do python -m nexus.cli.main $c --help >/dev/null && echo "$c ok"; done; python -m nexus.cli.main admin fs --help >/dev/null && echo "admin fs ok"; python -m nexus.cli.main cat --help | grep -q -- --offset && echo "cat range ok"`
Expected: every line prints `ok`.

- [ ] **Step 3: Lint/type the touched files**

Run: `ruff check src/nexus/cli/commands/file_ops.py src/nexus/cli/commands/admin.py src/nexus/cli/commands/__init__.py tests/unit/cli/test_fs_parity.py`
Expected: no errors (fix any).

- [ ] **Step 4: YAML + matrix sanity**

Run: `python -c "import yaml; yaml.safe_load(open('docs/architecture/api-rpc-surface-coverage.yaml')); print('ok')"`
Expected: `ok`

- [ ] **Step 5: Confirm no untracked FS gap remains**

Run: `grep -n "module: nexus_fs\|fs\." docs/architecture/api-rpc-surface-gaps.yaml || echo "no FS gaps - correct"`
Expected: `no FS gaps - correct` (every FS RPC now has a CLI + matrix row; the missing-surface gate is satisfied by direct implementation, no issues filed).

- [ ] **Step 6: Push branch and open PR**

```bash
git push -u origin issue-4133-full-profile-fs
gh pr create --base develop --title "docs+test(#4133): core FS/metadata/stream/batch RPC/CLI story" \
  --body "Closes #4133. Builds all missing FS CLI commands (stat, metadata, exists, read-bulk, rename-batch, rm-batch, cat --offset/--length/--stream, write --stream, admin fs *), CLI/RPC/syscall parity tests, gated E2E, guidance benchmarks, user-guide + full-profile.md FS section, coverage-matrix rows. Missing-surface gate satisfied by direct implementation (no issues filed). Spec: docs/superpowers/specs/2026-05-19-issue-4133-full-profile-fs-design.md"
```

- [ ] **Step 7: Run the golden-check skill** (repo requirement for PRs)

Invoke the `golden-check` skill; record/validate any golden queries/trajectories it flags for this branch.

---

## Self-Review (completed by plan author)

**Spec coverage:** lifecycle/batch/stream/range/lock guide → Tasks 14–15; CLI/RPC parity for every core method → Tasks 2–11; deprecated HTTP documented migration-only → Task 14/15 text + (E2E exercises gRPC path); missing CLI built not filed → Tasks 2–10 + Task 17 Step 5 gate check; benchmarks for hot-path groups → Task 13; coverage matrix → Task 12; correctness assertions #1–#8 → Tasks 8/11 (round-trip id, range==slice, batch shape, lock cycle, cross-path parity, ETag/OCC, denial, admin-only). All spec sections map to a task.

**Placeholder scan:** the only intentional fill-ins are the three `<bench:...>` values in Task 14, explicitly sourced from Task 13 Step 2's captured medians (instructed, not a TODO). "Plan-time check" notes are verification-of-real-signatures instructions with concrete commands, not deferred work.

**Type/name consistency:** command function names are stable across tasks and registration (`stat_cmd`, `metadata_cmd`, `exists_cmd`, `read_bulk_cmd`, `rename_batch_cmd`, `rm_batch_cmd`, `admin_fs*`); click names (`stat`,`metadata`,`exists`,`read-bulk`,`rename-batch`,`rm-batch`,`admin fs ...`) consistent between impl, `__init__.py` registration, and Task 17 smoke. RPC signatures match the verified "Ground-truth signatures" block.
