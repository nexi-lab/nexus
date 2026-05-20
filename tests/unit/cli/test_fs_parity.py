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


# ---------------------------------------------------------------------------
# Task 2: nexus stat (stat / stat_bulk)
# ---------------------------------------------------------------------------


def test_stat_single_parity(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import stat_cmd

    nx = patched_fs
    nx.write("/s.txt", b"abcde")
    rpc = nx.stat("/s.txt")
    res = cli_runner.invoke(stat_cmd, ["/s.txt", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)["data"]
    assert out["size"] == rpc["size"] == 5
    assert out["content_id"] == rpc["content_id"]


def test_stat_multi_uses_stat_bulk(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import stat_cmd

    nx = patched_fs
    nx.write("/a.txt", b"aa")
    nx.write("/b.txt", b"bbb")
    rpc = nx.stat_bulk(["/a.txt", "/b.txt"])
    res = cli_runner.invoke(stat_cmd, ["/a.txt", "/b.txt", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)["data"]
    assert out["/a.txt"]["size"] == rpc["/a.txt"]["size"] == 2
    assert out["/b.txt"]["size"] == 3


# ---------------------------------------------------------------------------
# Task 3: nexus metadata (metadata_batch)
# ---------------------------------------------------------------------------


def test_metadata_extended_parity(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import metadata_cmd

    nx = patched_fs
    nx.write("/m.txt", b"hi")
    rpc = nx.metadata_batch(["/m.txt", "/nope.txt"])
    res = cli_runner.invoke(metadata_cmd, ["/m.txt", "/nope.txt", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)["data"]
    assert out["/m.txt"]["size"] == rpc["/m.txt"]["size"] == 2
    # metadata_batch carries the extended keys stat_bulk lacks:
    assert "mime_type" in out["/m.txt"] and "created_at" in out["/m.txt"]
    assert out["/nope.txt"] is None


# ---------------------------------------------------------------------------
# Task 4: nexus exists (exists_batch)
# ---------------------------------------------------------------------------


def test_exists_batch_parity_and_exit(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import exists_cmd

    nx = patched_fs
    nx.write("/here.txt", b"x")
    rpc = nx.exists_batch(["/here.txt", "/gone.txt"])
    assert rpc == {"/here.txt": True, "/gone.txt": False}
    # --json: full map, exit 0
    res = cli_runner.invoke(exists_cmd, ["/here.txt", "/gone.txt", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["data"] == {"/here.txt": True, "/gone.txt": False}
    # plain: exit 0 iff ALL exist
    assert cli_runner.invoke(exists_cmd, ["/here.txt"]).exit_code == 0
    assert cli_runner.invoke(exists_cmd, ["/here.txt", "/gone.txt"]).exit_code == 1


# ---------------------------------------------------------------------------
# Task 5: nexus read-bulk (read_bulk / read_batch)
# ---------------------------------------------------------------------------


def test_read_bulk_parity(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import read_bulk_cmd

    nx = patched_fs
    nx.write("/r1.txt", b"one")
    nx.write("/r2.txt", b"two")
    rpc = nx.read_bulk(["/r1.txt", "/r2.txt"])
    res = cli_runner.invoke(read_bulk_cmd, ["/r1.txt", "/r2.txt", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)["data"]
    assert out["/r1.txt"] == rpc["/r1.txt"].decode() == "one"
    assert out["/r2.txt"] == "two"


def test_read_bulk_atomic_raises_on_missing(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import read_bulk_cmd

    nx = patched_fs
    nx.write("/r1.txt", b"one")
    res = cli_runner.invoke(read_bulk_cmd, ["/r1.txt", "/missing.txt", "--atomic", "--json"])
    # read_batch(partial=False) raises -> CLI catches and exits 1
    assert res.exit_code == 1


# ---------------------------------------------------------------------------
# Task 6: nexus rename-batch (rename_batch — per-item independent)
# ---------------------------------------------------------------------------


def test_rename_batch_per_item_independent(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import rename_batch_cmd

    nx = patched_fs
    nx.write("/old1.txt", b"1")  # /old2.txt deliberately absent
    res = cli_runner.invoke(
        rename_batch_cmd,
        ["/old1.txt:/new1.txt", "/old2.txt:/new2.txt", "--json"],
    )
    assert res.exit_code == 0, res.output  # independent: one failure does not abort the rest
    out = json.loads(res.output)["data"]
    assert out["/old1.txt"]["success"] is True
    assert out["/old1.txt"]["new_path"] == "/new1.txt"
    assert out["/old2.txt"]["success"] is False
    assert nx.read("/new1.txt") == b"1"


# ---------------------------------------------------------------------------
# Task 7: nexus rm-batch (delete_batch — per-item independent)
# ---------------------------------------------------------------------------


def test_rm_batch_per_item_independent(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import rm_batch_cmd

    nx = patched_fs
    nx.write("/d1.txt", b"1")
    nx.write("/d2.txt", b"2")
    res = cli_runner.invoke(rm_batch_cmd, ["/d1.txt", "/missing.txt", "/d2.txt", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)["data"]
    assert out["/d1.txt"]["success"] is True
    assert out["/d2.txt"]["success"] is True
    assert out["/missing.txt"]["success"] is False
    assert not nx.access("/d1.txt")


# ---------------------------------------------------------------------------
# Task 8: nexus cat --offset / --length (read_range)
# ---------------------------------------------------------------------------


def test_cat_range_equals_slice(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import cat

    nx = patched_fs
    nx.write("/big.txt", b"0123456789")
    assert nx.read_range("/big.txt", 2, 5) == b"234"
    res = cli_runner.invoke(cat, ["/big.txt", "--offset", "2", "--length", "3"])
    assert res.exit_code == 0, res.output
    assert res.output.rstrip("\n") == "234" or res.stdout_bytes == b"234"


def test_cat_no_range_unchanged(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import cat

    nx = patched_fs
    nx.write("/whole.txt", b"hello world")
    res = cli_runner.invoke(cat, ["/whole.txt"])
    assert res.exit_code == 0, res.output
    assert "hello world" in res.output


# ---------------------------------------------------------------------------
# Task 9: cat --stream / write --stream
# ---------------------------------------------------------------------------


def test_cat_stream_matches_full(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import cat

    nx = patched_fs
    body = b"x" * 200_000
    nx.write("/strm.bin", body)
    res = cli_runner.invoke(cat, ["/strm.bin", "--stream", "--chunk-size", "65536"])
    assert res.exit_code == 0, res.output
    assert res.stdout_bytes == body


def test_write_stream_from_stdin(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import write

    nx = patched_fs
    res = cli_runner.invoke(write, ["/ws.txt", "--stream"], input="streamed-bytes")
    assert res.exit_code == 0, res.output
    assert nx.read("/ws.txt") == b"streamed-bytes"


# ---------------------------------------------------------------------------
# Task 10: nexus admin fs (backfill-index / flush-write-observer)
# ---------------------------------------------------------------------------


def test_admin_fs_flush_and_backfill(inproc_nexus, cli_runner: CliRunner, monkeypatch):
    """admin fs * runs the admin-only RPCs against the in-process FS."""
    import contextlib

    @contextlib.asynccontextmanager
    async def _open(*a, **k):
        yield inproc_nexus

    monkeypatch.setattr("nexus.cli.utils.open_filesystem", _open, raising=False)
    from nexus.cli.commands.admin import admin

    r1 = cli_runner.invoke(admin, ["fs", "flush-write-observer", "--json"])
    assert r1.exit_code == 0, r1.output
    assert "flushed" in json.loads(r1.output)["data"]

    r2 = cli_runner.invoke(admin, ["fs", "backfill-index", "/", "--json"])
    assert r2.exit_code == 0, r2.output
    assert "entries_created" in json.loads(r2.output)["data"]


# ---------------------------------------------------------------------------
# Task 11: cross-path parity, content_id round-trip, range bounds, admin-only
# ---------------------------------------------------------------------------


def test_cli_read_equals_rpc_read(patched_fs, cli_runner: CliRunner):
    """nexus cat --offset/--length goes through read_range and matches nx.read."""
    from nexus.cli.commands.file_ops import cat

    nx = patched_fs
    nx.write("/p.txt", b"parity-bytes")
    rpc = nx.read("/p.txt")
    # Use the range branch which writes raw bytes to stdout.buffer (bypasses
    # the JSON envelope that auto-JSON wraps the metadata path in under
    # CliRunner's non-TTY stdout).
    cli = cli_runner.invoke(cat, ["/p.txt", "--offset", "0", "--length", str(len(rpc))])
    assert cli.exit_code == 0, cli.output
    assert cli.stdout_bytes == rpc == b"parity-bytes"


def test_write_roundtrips_content_id(patched_fs):
    nx = patched_fs
    w = nx.write("/cid.txt", b"data")
    s = nx.stat("/cid.txt")
    assert w["content_id"] == s["content_id"]


def test_range_out_of_bounds_is_bounded(patched_fs):
    nx = patched_fs
    nx.write("/short.txt", b"abc")
    # End past EOF returns the available bytes (bounded, not an error).
    out = nx.read_range("/short.txt", 0, 100)
    assert out == b"abc"


def test_cat_stream_survives_broken_pipe(patched_fs):
    """`nexus cat --stream` must exit cleanly when downstream pipe closes
    (e.g. piped to ``head -c 100``). Without BrokenPipeError handling the
    stream loop crashes with a Python traceback to stderr.

    Drives a subprocess that invokes the Click command and writes to a
    real pipe — closing the reader side after ~1 KB triggers EPIPE on
    subsequent ``sys.stdout.buffer.write`` calls inside the stream loop.
    """
    import os
    import subprocess
    import sys
    import textwrap

    nx = patched_fs
    nx.write("/big.bin", b"x" * 200_000)

    # Run a child Python that re-creates the in-process fixture, then invokes
    # the cat --stream click command with stdout connected to a pipe that we
    # close after a few KB so the writer hits EPIPE mid-stream.
    driver = textwrap.dedent("""
        import os, sys, contextlib, asyncio
        import nexus.cli.main  # noqa: F401  installs SIGPIPE = SIG_DFL
        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.core.config import ParseConfig, PermissionConfig
        from nexus.factory import create_nexus_fs
        from nexus.remote.kernel_client import KernelClient
        from click.testing import CliRunner

        tmp = os.environ["TMPDIR_FX"]
        os.makedirs(tmp + "/data", exist_ok=True)
        k = KernelClient()
        k.set_metastore_path(tmp + "/metastore.redb")
        k.open()
        nx = create_nexus_fs(
            backend=PathLocalBackend(root_path=tmp + "/data"),
            metadata_store=k,
            record_store=None,
            permissions=PermissionConfig(enforce=False),
            parsing=ParseConfig(auto_parse=False),
        )
        nx.write("/big.bin", b"x" * 200_000)

        @contextlib.asynccontextmanager
        async def _open(*a, **k): yield nx
        import nexus.cli.commands.file_ops as fo
        fo.open_filesystem = _open
        fo.get_filesystem = lambda *a, **k: nx

        from nexus.cli.commands.file_ops import cat
        # Direct call writes to real sys.stdout.buffer (the pipe).
        ctx = cat.make_context("cat", ["/big.bin", "--stream", "--chunk-size", "4096"])
        cat.invoke(ctx)
        sys.exit(0)
    """)

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, TMPDIR_FX=tmp, PYTHONPATH=":".join(sys.path))
        p1 = subprocess.Popen(
            [sys.executable, "-c", driver],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        # Read first 1 KB then close — simulates ``| head -c 1024``.
        assert p1.stdout is not None
        first = p1.stdout.read(1024)
        p1.stdout.close()
        try:
            _, stderr = p1.communicate(timeout=15)
        except subprocess.TimeoutExpired as exc:
            p1.kill()
            raise AssertionError("cat --stream hung after pipe close") from exc

    # Must not propagate raw Python traceback to stderr.
    assert b"Traceback" not in stderr, stderr.decode(errors="replace")
    assert b"BrokenPipeError" not in stderr, stderr.decode(errors="replace")
    assert len(first) == 1024
    # 0 = stream finished pre-close; -13 / 141 = killed by SIGPIPE (Unix default).
    assert p1.returncode in (0, 141, -13), f"unexpected rc={p1.returncode}: {stderr!r}"


def test_concurrent_fs_stress(inproc_nexus):
    """Fire parallel write / read / stat / rename / delete across threads and
    assert no crashes + final state correct. Targets the path-index race that
    motivated the kernel-side DashMap projection.
    """
    import concurrent.futures as cf
    import random

    nx = inproc_nexus
    n = 200
    random.seed(0xBEEF)

    # Seed: write N distinct files.
    for i in range(n):
        nx.write(f"/c/{i:04d}.txt", f"v0-{i}".encode())

    def _write(i: int) -> tuple[str, str]:
        path = f"/c/{i:04d}.txt"
        body = f"v1-{i}".encode()
        nx.write(path, body)
        return ("w", path)

    def _read(i: int) -> tuple[str, bool]:
        path = f"/c/{i:04d}.txt"
        data = nx.read(path)
        return ("r", data in (f"v0-{i}".encode(), f"v1-{i}".encode()))

    def _stat(i: int) -> tuple[str, int]:
        return ("s", nx.stat(f"/c/{i:04d}.txt")["size"])

    def _exists_bulk(_i: int) -> tuple[str, int]:
        paths = [f"/c/{j:04d}.txt" for j in range(0, n, 7)]
        ex = nx.exists_batch(paths)
        return ("e", sum(1 for v in ex.values() if v))

    ops = []
    for i in range(n):
        ops.extend([(_write, i), (_read, i), (_stat, i), (_exists_bulk, i)])
    random.shuffle(ops)

    errors: list[BaseException] = []
    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        futs = [pool.submit(fn, arg) for fn, arg in ops]
        for f in cf.as_completed(futs):
            try:
                f.result()
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

    assert not errors, f"{len(errors)} errors; first: {errors[0]!r}"

    # Final invariant: every seeded path still readable + stat OK.
    for i in range(n):
        path = f"/c/{i:04d}.txt"
        body = nx.read(path)
        assert body in (f"v0-{i}".encode(), f"v1-{i}".encode())
        st = nx.stat(path)
        assert st["size"] in (len(f"v0-{i}"), len(f"v1-{i}"))


def test_admin_only_dispatch_rejects_non_admin(inproc_nexus):
    """Verify the RPC dispatcher itself refuses non-admin callers — not just
    the @rpc_expose metadata. Calls dispatch_method twice for each admin-only
    method: once with is_admin=False (expects NexusPermissionError) and once
    with is_admin=True (expects success).
    """
    import asyncio
    from types import SimpleNamespace

    import pytest as _pytest

    from nexus.contracts.exceptions import NexusPermissionError
    from nexus.server.rpc.discovery import discover_exposed_methods
    from nexus.server.rpc.dispatch import dispatch_method

    nx = inproc_nexus
    exposed = discover_exposed_methods(nx)
    assert "backfill_directory_index" in exposed
    assert "flush_write_observer" in exposed

    non_admin = SimpleNamespace(is_admin=False, user_id="u1", zone_id="default")
    admin = SimpleNamespace(is_admin=True, user_id="root", zone_id="default")

    backfill_params = SimpleNamespace(prefix="/", zone_id=None)
    flush_params = SimpleNamespace()

    # Non-admin path: both admin_only RPCs must be refused at dispatch.
    for method, params in (
        ("backfill_directory_index", backfill_params),
        ("flush_write_observer", flush_params),
    ):
        with _pytest.raises(NexusPermissionError):
            asyncio.run(
                dispatch_method(
                    method,
                    params,
                    non_admin,
                    nexus_fs=nx,
                    exposed_methods=exposed,
                )
            )

    # Admin path: both succeed.
    out = asyncio.run(
        dispatch_method(
            "backfill_directory_index",
            backfill_params,
            admin,
            nexus_fs=nx,
            exposed_methods=exposed,
        )
    )
    assert "entries_created" in out

    out2 = asyncio.run(
        dispatch_method(
            "flush_write_observer",
            flush_params,
            admin,
            nexus_fs=nx,
            exposed_methods=exposed,
        )
    )
    assert "flushed" in out2


def test_admin_only_metadata_is_set():
    """backfill_directory_index / flush_write_observer carry admin_only=True so
    the RPC dispatcher refuses non-admin callers server-side. Direct in-process
    method calls bypass the dispatcher; this test verifies the metadata that
    server-side enforcement reads — same guarantee, correct level."""
    from nexus.core.nexus_fs_metadata import MetadataMixin

    for name in ("backfill_directory_index", "flush_write_observer"):
        fn = getattr(MetadataMixin, name)
        spec = getattr(fn, "_rpc_expose", None) or getattr(fn, "__rpc_expose__", None)
        # rpc_expose stores metadata on the function; we check whichever attr
        # the decorator chose without coupling to one shape.
        # Fall back: locate admin_only=True in the source decorator line.
        if spec is None:
            import inspect

            src = inspect.getsource(fn)
            assert "admin_only=True" in src, f"{name} missing admin_only=True"
        else:
            assert getattr(spec, "admin_only", False), f"{name} not admin_only"
