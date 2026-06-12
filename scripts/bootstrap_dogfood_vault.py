#!/usr/bin/env python3
"""One-time tool to create an empty encrypted vault DB for the dogfood path.

Run once per master key, by the kernel-team lead. Boots an ephemeral
`nexusd-cluster + vault.dylib` against a tempdir with a freshly generated
master key in env, waits until vault initializes its redb file, stops the
cluster, and moves the empty initialized DB into
`data/vault-signing/vault.redb`.

After this script returns:

  1. Register the printed master key as the `VAULT_SIGNING_MASTER_KEY` GH
     org secret:
       gh secret set VAULT_SIGNING_MASTER_KEY --org nexi-lab --body '<key>'
  2. Stage + commit the new `data/vault-signing/vault.redb` and push the PR.
  3. Provision keys via `scripts/provision_dogfood_key.py` (one-per-PR).

Refuses to run if `data/vault-signing/vault.redb` already exists —
overwriting would obliterate every committed signing key. Rotation has a
separate procedure (see design doc).

The generated key prints to stderr, NOT stdout, so piping the script's
stdout into a file doesn't accidentally land the master key on disk. The
master key is shredded from process memory before exit on a best-effort
basis (same `ctypes.memset` pattern as `provision_dogfood_key.py`).

CLI:
  bootstrap_dogfood_vault.py \
    --nexusd-cluster <path-to-nexusd-cluster-binary> \
    --vault-plugin-dir <dir-with-libnexus_vault.so + .sig>

See `docs/superpowers/specs/2026-06-12-encrypted-in-git-vault-dogfood-design.md`.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_READY_MARKER = "cluster.ready"
DEFAULT_READY_TIMEOUT_SEC = 60
DEFAULT_VAULT_PORT = 2126
MASTER_KEY_BYTES = 32


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _target_db_path(root: Path) -> Path:
    return root / "data" / "vault-signing" / "vault.redb"


def _shred_bytes(buf: bytes) -> None:
    """Best-effort in-place zero of a bytes buffer's backing memory.

    Mirrors the pattern in `provision_dogfood_key.py`. Reduces the window
    the master key sits live in process RSS.
    """
    if not buf:
        return
    addr = ctypes.cast(ctypes.c_char_p(buf), ctypes.c_void_p).value
    if addr:
        ctypes.memset(addr, 0, len(buf))


def _wait_for_ready(
    data_dir: Path,
    ready_marker: str,
    timeout_sec: int,
    proc: subprocess.Popen[bytes],
) -> None:
    deadline = time.monotonic() + timeout_sec
    marker_path = data_dir / ready_marker
    redb_path = data_dir / "vault.redb"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(
                f"nexusd-cluster exited prematurely with code {proc.returncode} "
                f"before ready marker appeared"
            )
        if marker_path.is_file():
            return
        # Fallback: vault has flushed an initialized redb file. Use a
        # non-zero size as a heuristic for "init complete enough to copy".
        if redb_path.is_file() and redb_path.stat().st_size > 0:
            # Give vault a moment to finish post-init bookkeeping if it's
            # going to write the ready marker later.
            time.sleep(1)
            return
        time.sleep(0.5)
    raise SystemExit(f"timed out after {timeout_sec}s waiting for {marker_path} or {redb_path}")


def _stop_cluster(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "posix":
        # start_new_session=True put it in its own process group; signal the
        # whole group so raft / plugin loader subprocesses tear down too.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Initialize the encrypted-in-git vault DB for plugin signing.",
    )
    p.add_argument(
        "--nexusd-cluster",
        default="nexusd-cluster",
        help="Path to the nexusd-cluster binary (default: 'nexusd-cluster' on PATH).",
    )
    p.add_argument(
        "--vault-plugin-dir",
        type=Path,
        required=True,
        help="Directory containing libnexus_vault.<ext> + .sig. Passed as --plugin-dir.",
    )
    p.add_argument(
        "--ready-marker",
        default=DEFAULT_READY_MARKER,
        help=f"Filename under data-dir vault writes when ready (default: {DEFAULT_READY_MARKER}).",
    )
    p.add_argument(
        "--ready-timeout-sec",
        type=int,
        default=DEFAULT_READY_TIMEOUT_SEC,
        help=f"Seconds to wait for ready marker (default: {DEFAULT_READY_TIMEOUT_SEC}).",
    )
    args = p.parse_args(argv)

    root = _repo_root()
    target = _target_db_path(root)
    if target.exists():
        raise SystemExit(
            f"refusing to clobber existing vault DB: {target.relative_to(root)}\n"
            f"This script is one-time. Rotation has a separate procedure — see "
            f"docs/superpowers/specs/2026-06-12-encrypted-in-git-vault-dogfood-design.md."
        )

    plugin_dir = args.vault_plugin_dir.resolve()
    if not plugin_dir.is_dir():
        raise SystemExit(f"vault plugin dir does not exist: {plugin_dir}")
    dylib_candidates = (
        plugin_dir / "libnexus_vault.so",
        plugin_dir / "libnexus_vault.dylib",
        plugin_dir / "nexus_vault.dll",
    )
    if not any(c.is_file() for c in dylib_candidates):
        raise SystemExit(
            f"vault plugin dir has no libnexus_vault.{{so,dylib}} or nexus_vault.dll: {plugin_dir}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)

    raw_master = os.urandom(MASTER_KEY_BYTES)
    master_b64 = base64.b64encode(raw_master).decode("ascii")

    with tempfile.TemporaryDirectory(prefix="bootstrap-dogfood-vault-") as td:
        data_dir = Path(td)

        env = os.environ.copy()
        env["VAULT_MASTER_KEY"] = master_b64

        popen_kwargs: dict[str, object] = {
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            [
                args.nexusd_cluster,
                "--no-tls",
                "--data-dir",
                str(data_dir),
                "--plugin-dir",
                str(plugin_dir),
                "--bootstrap-mode",
                "static",
            ],
            **popen_kwargs,
        )
        try:
            _wait_for_ready(data_dir, args.ready_marker, args.ready_timeout_sec, proc)
        finally:
            _stop_cluster(proc)

        produced = data_dir / "vault.redb"
        if not produced.is_file():
            raise SystemExit(f"vault did not produce {produced} — check cluster stderr above")
        if produced.stat().st_size == 0:
            raise SystemExit(f"vault produced zero-byte {produced}")

        shutil.move(str(produced), str(target))

    print(
        f"\nWrote initialized vault DB: {target.relative_to(root)}\n"
        f"\nNext steps:\n"
        f"  1. Register the master key as a GH org secret:\n"
        f"       gh secret set VAULT_SIGNING_MASTER_KEY --org nexi-lab --body '<master-key-below>'\n"
        f"  2. Stage and commit the DB:\n"
        f"       git add {target.relative_to(root)}\n"
        f"       git commit -m 'feat(vault-signing): initial encrypted DB'\n"
        f"  3. Push and open a PR; CODEOWNERS gates kernel-team review.\n"
        f"\nMASTER KEY (base64, 32 raw bytes) — save this NOW, it is NOT recoverable:\n"
        f"{master_b64}\n",
        file=sys.stderr,
    )

    _shred_bytes(raw_master)
    return 0


if __name__ == "__main__":
    sys.exit(main())
