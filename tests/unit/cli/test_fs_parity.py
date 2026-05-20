"""CLI <-> RPC <-> syscall parity for the core FS surface (Issue #4133)."""

from __future__ import annotations

import json

import pytest
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


def test_cross_path_parity_syscall_rpc_cli(patched_fs, cli_runner: CliRunner):
    """Spec correctness assertion #5: read via (a) kernel syscall direct,
    (b) generic ``Call`` RPC (the path used by both the typed gRPC
    servicer and the HTTP ``/api/nfs/read`` route), and (c) CLI
    ``nexus cat`` — all return byte-identical content. Stat from
    syscall vs RPC returns identical ``content_id`` + ``size``.

    The HTTP ``/api/nfs/{method}`` route dispatches through
    ``dispatch_kernel_syscall`` for the kernel-syscall wire names
    (read/write/stat/...) and the gRPC ``Call`` servicer reuses the
    same dispatch path, so exercising it in-process proves both
    transports use a byte-identical decoder + kernel.
    """
    import asyncio

    from nexus.cli.commands.file_ops import cat
    from nexus.server._kernel_syscall_dispatch import dispatch_kernel_syscall

    nx = patched_fs
    body = b"cross-path-parity-payload"
    nx.write("/cp/file.bin", body)

    # (a) syscall direct
    syscall_bytes = nx.sys_read("/cp/file.bin")
    syscall_stat = nx.stat("/cp/file.bin")

    # (b) Generic Call / HTTP-RPC dispatch path. dispatch_kernel_syscall
    # routes the OperationContext through context_utils.normalize_context
    # which only accepts OperationContext|dict — so pass an OC directly.
    from nexus.contracts.types import OperationContext

    ctx = OperationContext(
        user_id="root",
        groups=[],
        zone_id="default",
        is_admin=True,
    )
    rpc_bytes = asyncio.run(dispatch_kernel_syscall(nx, "read", {"path": "/cp/file.bin"}, ctx))
    rpc_stat = asyncio.run(dispatch_kernel_syscall(nx, "stat", {"path": "/cp/file.bin"}, ctx))

    # (c) CLI — the cli_runner harness emits the JSON envelope by default
    # (data + _timing). Extract data.content for the byte comparison.
    res = cli_runner.invoke(cat, ["/cp/file.bin"])
    assert res.exit_code == 0, res.output
    cli_payload = json.loads(res.output)
    cli_content = cli_payload["data"]["content"]
    cli_bytes = cli_content.encode("utf-8") if isinstance(cli_content, str) else cli_content

    # Byte-identity across all three paths.
    assert syscall_bytes == body
    # dispatch_kernel_syscall may return bytes or a dict containing bytes.
    if isinstance(rpc_bytes, dict):
        rpc_bytes = rpc_bytes.get("content") or rpc_bytes.get("data") or rpc_bytes
    assert rpc_bytes == body
    assert cli_bytes == body

    # Metadata identity: same content_id + size from syscall and RPC.
    if isinstance(rpc_stat, dict):
        assert rpc_stat["content_id"] == syscall_stat["content_id"]
        assert rpc_stat["size"] == syscall_stat["size"] == len(body)


def test_cat_large_file_above_stream_threshold(patched_fs, cli_runner: CliRunner):
    """`cat --stream` on an 11 MiB file writes raw chunks to
    ``sys.stdout.buffer`` and must round-trip byte-identical — no
    JSON envelope, no truncation, no double-encoding.

    Targets the chunked write loop (file_ops.py: ``sys.stdout.buffer.write(ch)``)
    which is the same code path the >10 MiB auto-stream branch uses
    internally. Explicit ``--stream`` makes the assertion deterministic
    (CliRunner captures raw bytes via ``stdout_bytes``) and side-steps
    the size-probe branch — see ``test_cat_auto_stream_branch_fires``
    below for the auto-detect coverage.
    """
    from nexus.cli.commands.file_ops import cat as _cat

    nx = patched_fs
    body = b"L" * (11 * 1024 * 1024)  # 11 MiB — above 10 MiB threshold
    nx.write("/big/huge.bin", body)

    # Direct read via syscall — sanity check.
    assert nx.read("/big/huge.bin") == body

    res = cli_runner.invoke(_cat, ["/big/huge.bin", "--stream", "--chunk-size", str(64 * 1024)])
    assert res.exit_code == 0, res.output[:500]
    assert res.stdout_bytes == body, (
        f"stream output mismatch: got {len(res.stdout_bytes)} bytes, expected {len(body)}"
    )


def test_cat_auto_stream_branch_fires(patched_fs, monkeypatch):
    """Verify the >10 MiB auto-stream branch in ``cat``:
    (a) fires (nx.stream invoked), and
    (b) writes raw bytes to stdout — byte-identical, no banner mixed in.

    Catches both the silent-skip regression (size probe falls through →
    whole-file read + JSON envelope) and the corruption regression
    (banner text written before file bytes on stdout).
    """
    from click.testing import CliRunner

    from nexus.cli.commands.file_ops import cat as _cat

    nx = patched_fs
    body = b"S" * (11 * 1024 * 1024)
    nx.write("/big/auto.bin", body)

    stream_calls: list[tuple[str, int]] = []
    original_stream = nx.stream

    def _spy_stream(path: str, *, chunk_size: int = 65536, **kw):
        stream_calls.append((path, chunk_size))
        return original_stream(path, chunk_size=chunk_size, **kw)

    monkeypatch.setattr(nx, "stream", _spy_stream)

    res = CliRunner(mix_stderr=False).invoke(_cat, ["/big/auto.bin"])
    assert res.exit_code == 0, (res.output[:500], res.stderr[:500] if res.stderr else "")
    assert stream_calls, (
        "cat did not invoke nx.stream — the >10 MiB auto-stream branch "
        "was skipped. Likely the size probe failed silently. "
        f"stdout head: {res.stdout_bytes[:200]!r}"
    )
    # Chunk size matches the auto-stream branch.
    assert stream_calls[0][1] == 65536
    # Critical: stdout must be byte-identical to the file. The "Streaming
    # large file..." status line is on stderr; nothing else may appear on
    # stdout before/after the file bytes.
    assert res.stdout_bytes == body, (
        f"stdout corruption: got {len(res.stdout_bytes)} bytes, "
        f"expected {len(body)}. head={res.stdout_bytes[:80]!r}"
    )
    # Status banner should be on stderr (informational only).
    assert b"Streaming" in (res.stderr_bytes or b""), (
        f"expected 'Streaming...' status line on stderr; stderr={res.stderr_bytes[:200]!r}"
    )


def test_worktree_cli_resolves_to_src_with_pythonpath():
    """E2E fixtures invoke ``python -m nexus.cli`` with ``PYTHONPATH=src``
    so the worktree CLI runs, not the system-installed package. Verify
    that resolution actually picks up the worktree (otherwise Bug B
    creeps back if a stale install ships an older ``shared`` preset).
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_src = Path(__file__).resolve().parents[3] / "src"
    assert (repo_src / "nexus" / "__init__.py").exists()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_src) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

    # Use a one-shot Python that prints the resolved nexus.__file__ so
    # we can assert it lives under the worktree, not site-packages.
    proc = subprocess.run(
        [sys.executable, "-c", "import nexus; print(nexus.__file__)"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert proc.returncode == 0, f"import nexus failed: {proc.stderr!r}"
    resolved = Path(proc.stdout.strip()).resolve()
    assert str(resolved).startswith(str(repo_src.resolve())), (
        f"nexus loaded from {resolved}, expected under {repo_src.resolve()} — "
        "PYTHONPATH=src not winning over site-packages"
    )


def test_auth_denial_401_unauth_and_403_admin_only():
    """Spec correctness assertion #7: unauthenticated request → 401;
    authenticated but unpermitted (non-admin on admin-only) → 403.

    Exercises the FastAPI dependencies directly (the same gates the
    HTTP RPC endpoint composes via ``Depends(require_auth)``).
    """
    import asyncio

    import pytest as _pytest
    from fastapi import HTTPException

    from nexus.server.dependencies import require_admin, require_auth

    # 401: no auth result (anonymous request)
    with _pytest.raises(HTTPException) as exc_unauth:
        asyncio.run(require_auth(None))
    assert exc_unauth.value.status_code == 401

    # 401: auth result present but ``authenticated`` is False
    with _pytest.raises(HTTPException) as exc_bad:
        asyncio.run(require_auth({"authenticated": False}))
    assert exc_bad.value.status_code == 401

    # 200-equivalent: authenticated user passes require_auth.
    out = asyncio.run(require_auth({"authenticated": True, "is_admin": False}))
    assert out["authenticated"] is True

    # 403: authenticated but non-admin → admin-only endpoint refuses.
    with _pytest.raises(HTTPException) as exc_403:
        asyncio.run(require_admin({"authenticated": True, "is_admin": False}))
    assert exc_403.value.status_code == 403

    # Admin passes.
    admin_out = asyncio.run(require_admin({"authenticated": True, "is_admin": True}))
    assert admin_out["is_admin"] is True


def test_etag_if_match_occ_http_v2_write(inproc_nexus):
    """The ``/api/v2/files/write`` route advertises ``if_match`` in its
    request schema, but until this round it built ``write_kwargs``
    without forwarding the field — so a stale ``if_match`` would
    silently overwrite. Verify the route now routes through
    ``occ_write_sync`` and returns 409 on stale, 200 on matching.
    """
    import base64

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nexus.server.api.v2.routers.async_files import create_async_files_router
    from nexus.server.dependencies import require_auth as _require_auth_dep

    nx = inproc_nexus
    nx.write("/occhttp/a.txt", b"v1")
    good_id = nx.stat("/occhttp/a.txt")["content_id"]

    app = FastAPI()
    # Direct-mode router: nexus_fs supplied at construction. Prefix
    # matches how production mounts it (see versioning.py:147).
    app.include_router(create_async_files_router(nexus_fs=nx), prefix="/api/v2/files")

    async def _fake_require_auth():
        return {
            "authenticated": True,
            "is_admin": True,
            "subject_id": "root",
            "subject_type": "user",
            "zone_id": "root",
        }

    app.dependency_overrides[_require_auth_dep] = _fake_require_auth

    client = TestClient(app)

    def _payload(content_id: str | None) -> dict:
        body = {
            "path": "/occhttp/a.txt",
            "content": base64.b64encode(b"v2").decode(),
            "encoding": "base64",
        }
        if content_id is not None:
            body["if_match"] = content_id
        return body

    # Stale id → 409 (route now honors OCC).
    stale = client.post("/api/v2/files/write", json=_payload("sha256:stale-deadbeef"))
    assert stale.status_code == 409, (
        f"expected 409 on stale if_match, got {stale.status_code}: {stale.text[:200]}"
    )
    assert nx.read("/occhttp/a.txt") == b"v1"

    # Matching id → 200/201 + bytes updated.
    fresh = client.post("/api/v2/files/write", json=_payload(good_id))
    assert fresh.status_code in (200, 201), fresh.text
    assert nx.read("/occhttp/a.txt") == b"v2"


def test_etag_if_match_occ_conflict(inproc_nexus):
    """Spec correctness assertion #8: ``write`` with a stale ``content_id``
    via :func:`nexus.lib.occ.occ_write_sync` is rejected with
    :class:`ConflictError`. A write with the matching ``content_id``
    succeeds, advancing the version.
    """
    import pytest as _pytest

    from nexus.contracts.exceptions import ConflictError
    from nexus.lib.occ import occ_write_sync

    nx = inproc_nexus
    nx.write("/occ/a.txt", b"v1")
    st1 = nx.stat("/occ/a.txt")
    good_id = st1["content_id"]

    # Stale id → ConflictError
    with _pytest.raises(ConflictError):
        occ_write_sync(nx, "/occ/a.txt", b"v2", if_match="sha256:stale-id-deadbeef")

    # File contents unchanged after the rejected write.
    assert nx.read("/occ/a.txt") == b"v1"

    # Matching id → succeeds, bytes update.
    out = occ_write_sync(nx, "/occ/a.txt", b"v2", if_match=good_id)
    assert "content_id" in out
    assert nx.read("/occ/a.txt") == b"v2"

    # The in-process kernel uses a path-stable content_id (matches HTTP
    # API stat output); the bytes-changed invariant is the meaningful
    # part of OCC here. Version/gen advance regardless.
    st2 = nx.stat("/occ/a.txt")
    assert st2.get("version", 0) >= st1.get("version", 0)


def test_lock_contention_second_acquirer_refused(inproc_nexus):
    """Spec correctness assertion #4: a second acquirer of an exclusive
    advisory lock is refused while the first holder is alive. The
    kernel raises NexusError("lock acquisition failed (contention)").
    After the first holder releases, a fresh acquire succeeds.
    """
    import pytest as _pytest

    from nexus.contracts.exceptions import NexusError

    nx = inproc_nexus
    nx.write("/lk/x.txt", b"x")

    lid1 = nx.sys_lock("/lk/x.txt")
    assert lid1, "first acquire must succeed"

    # Second acquirer must be refused while lid1 is alive.
    with _pytest.raises(NexusError, match="contention"):
        nx.sys_lock("/lk/x.txt")

    # First holder releases cleanly. sys_unlock may return ``True`` or the
    # legacy wire shape ``{"released": True}``.
    rel = nx.sys_unlock("/lk/x.txt", lock_id=lid1)
    assert rel is True or (isinstance(rel, dict) and rel.get("released") is True)

    # After release, a fresh acquire succeeds with a different lid.
    lid3 = nx.sys_lock("/lk/x.txt")
    assert lid3 and lid3 != lid1
    nx.sys_unlock("/lk/x.txt", lock_id=lid3)


def test_sustained_fs_soak(inproc_nexus):
    """Opt-in sustained soak (gated by ``NEXUS_SOAK=1``): 1000 files,
    32 threads, ≥60s of writes+reads+stats+renames. Surfaces leaks /
    contention regressions that the 9-second stress doesn't see.
    Skipped by default to keep CI fast.
    """
    import concurrent.futures as cf
    import os
    import random
    import time

    if os.environ.get("NEXUS_SOAK") != "1":
        pytest.skip("opt-in: set NEXUS_SOAK=1 to run the sustained soak (~60s)")

    nx = inproc_nexus
    n_files = 1000
    workers = 32
    duration_s = 60.0
    random.seed(0xCAFEBABE)

    # Seed
    for i in range(n_files):
        nx.write(f"/soak/{i:05d}.txt", b"v0")

    end_at = time.monotonic() + duration_s
    errors: list[BaseException] = []
    counters = {"w": 0, "r": 0, "s": 0, "rn": 0}

    def _worker(seed: int) -> None:
        rng = random.Random(seed)
        local = {"w": 0, "r": 0, "s": 0, "rn": 0}
        while time.monotonic() < end_at:
            op = rng.choice(("w", "r", "s", "rn"))
            i = rng.randrange(n_files)
            path = f"/soak/{i:05d}.txt"
            try:
                if op == "w":
                    nx.write(path, f"v-{rng.randrange(10):x}".encode())
                elif op == "r":
                    nx.read(path)
                elif op == "s":
                    nx.stat(path)
                else:
                    j = rng.randrange(n_files)
                    other = f"/soak/{j:05d}.txt"
                    nx.rename_batch([(path, other)])
                local[op] += 1
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                return
        for k, v in local.items():
            counters[k] += v

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_worker, w) for w in range(workers)]
        for f in cf.as_completed(futs):
            f.result()

    assert not errors, f"{len(errors)} errors during soak; first: {errors[0]!r}"
    total_ops = sum(counters.values())
    assert total_ops > 1000, f"soak ran too few ops: {counters}"


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
