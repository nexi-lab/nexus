"""Shared helpers for the sealed-keystore plugin-signing dogfood flow.

Used by:
  - scripts/provision_dogfood_key.py     — operator-side keystore writer
  - scripts/dogfood_keystore_fetch.py    — CI-side privkey extractor
  - tests/integration/dogfood_keystore_e2e.py — end-to-end test

Not a CLI entrypoint. The leading underscore marks it as a private
implementation detail of the three scripts above.

Functions cluster around three concerns:

  1. Master-key + keystore I/O — read env, load/save JSON keystore.
  2. Ephemeral cluster lifecycle — find binaries, boot, wait, kill.
  3. Vault proto compilation — generate `*_pb2*` at runtime so no
     stubs land in the source tree.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PROTO_REL = Path("rust/services/proto/nexus/secrets/v1/secrets.proto")
PROTO_IMPORT_ROOT_REL = Path("rust/services/proto")
DEFAULT_KEYSTORE_REL = Path("data/vault-signing")
MASTER_KEY_BYTES = 32
SIGNING_NAMESPACE = "signing-keys"

CLUSTER_READY_TIMEOUT_S = 30.0
CLUSTER_PORT_POLL_INTERVAL_S = 0.25


def repo_root() -> Path:
    here = Path(__file__).resolve()
    return here.parent.parent


def compile_proto_into(out_dir: Path) -> None:
    """Generate `nexus/secrets/v1/secrets_pb2{,_grpc}.py` under out_dir."""
    import grpc_tools
    from grpc_tools import protoc

    proto_root = repo_root() / PROTO_IMPORT_ROOT_REL
    proto_file = repo_root() / PROTO_REL
    if not proto_file.is_file():
        raise SystemExit(f"proto SSOT missing: {proto_file}")

    google_proto_root = Path(os.path.dirname(grpc_tools.__file__)) / "_proto"

    rc = protoc.main(
        [
            "grpc_tools.protoc",
            f"--proto_path={proto_root}",
            f"--proto_path={google_proto_root}",
            f"--python_out={out_dir}",
            f"--grpc_python_out={out_dir}",
            str(proto_file),
        ]
    )
    if rc != 0:
        raise SystemExit(f"protoc exited {rc} when compiling {proto_file}")


def shred_bytes(buf: bytes | bytearray) -> None:
    """Best-effort zero of a bytes buffer's backing memory.

    Python doesn't guarantee unique allocations, but for fresh
    `os.urandom` / `private_bytes_raw()` returns we have a unique
    block. Not bulletproof against forensic dumps, but reduces the
    privkey's resident time. No-op on empty input.
    """
    if not buf:
        return
    addr = ctypes.cast(ctypes.c_char_p(bytes(buf)), ctypes.c_void_p).value
    if addr:
        ctypes.memset(addr, 0, len(buf))


def read_master_key_from_env(env_var: str = "VAULT_SIGNING_MASTER_KEY") -> bytes:
    # Drop ALL whitespace (not just leading/trailing) — secrets pasted
    # through GitHub UI or shell pipelines occasionally carry embedded
    # \r, \n, or spaces that survive a plain .strip(). `validate=True`
    # in the next step rejects any surviving non-base64 char so a
    # genuinely garbled value still fails loudly.
    raw = "".join(os.environ.get(env_var, "").split())
    if not raw:
        raise SystemExit(f"{env_var} env var is required")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise SystemExit(f"{env_var} is not valid base64: {exc}") from exc
    if len(decoded) != MASTER_KEY_BYTES:
        raise SystemExit(f"{env_var} decodes to {len(decoded)} bytes; expected {MASTER_KEY_BYTES}")
    return decoded


def _find_default_path(env_var: str, candidates: list[Path]) -> Path | None:
    if env_path := os.environ.get(env_var, "").strip():
        p = Path(env_path)
        if p.is_file():
            return p
    for c in candidates:
        if c.is_file():
            return c
    return None


def default_nexusd_cluster() -> Path | None:
    return _find_default_path(
        "NEXUSD_CLUSTER",
        [Path("nexusd-cluster.exe"), Path("nexusd-cluster")],
    )


def default_vault_dylib() -> Path | None:
    name_candidates = [
        "libnexus_vault.so",
        "libnexus_vault.dylib",
        "nexus_vault.dll",
    ]
    target = repo_root() / "target"
    candidates = [
        target / profile / name for profile in ("release", "debug") for name in name_candidates
    ]
    return _find_default_path("VAULT_DYLIB_PATH", candidates)


def free_port() -> int:
    """Reserve an OS-assigned port. The chosen port races with cluster
    binding but it is the simplest portable way to pre-allocate one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, deadline: float) -> None:
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", port))
                return
            except (TimeoutError, ConnectionRefusedError, OSError):
                time.sleep(CLUSTER_PORT_POLL_INTERVAL_S)
    raise SystemExit(f"cluster did not start listening on 127.0.0.1:{port} within deadline")


def boot_cluster(
    *,
    nexusd_cluster: Path,
    vault_dylib: Path,
    master_key: bytes,
    data_dir: Path,
) -> tuple[subprocess.Popen[bytes], int]:
    """Spawn `nexusd-cluster` against a fresh `data_dir` with vault
    pre-staged. Blocks until the gRPC port accepts TCP.

    Side effects on `data_dir`:
      - Writes `vault/master.key` (32 raw bytes).
      - Copies `vault_dylib` into `plugins/<basename>`.

    Returns `(process, bound_port)`. Caller MUST call `kill_cluster()`
    in a `finally` to avoid orphan runners (CI cleans tempdirs but a
    half-alive cluster keeps a port).
    """
    vault_data = data_dir / "vault"
    vault_data.mkdir(parents=True, exist_ok=True)
    (vault_data / "master.key").write_bytes(master_key)

    plugin_dir = data_dir / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / vault_dylib.name).write_bytes(vault_dylib.read_bytes())
    # The cluster's plugin loader requires `<plugin>.sig` next to the
    # binary and Ed25519-verifies the dylib bytes against the embedded
    # `trusted_keys/*.pub`. Carry the sig that ships in the release
    # archive — the dylib without it is rejected at boot.
    sig_src = vault_dylib.with_name(vault_dylib.name + ".sig")
    if sig_src.is_file():
        (plugin_dir / sig_src.name).write_bytes(sig_src.read_bytes())

    port = free_port()
    env = os.environ.copy()
    env["NEXUS_DATA_DIR"] = str(data_dir)
    env["RUST_LOG"] = env.get("RUST_LOG", "warn")

    # Use the `serve-local` subcommand rather than hand-composing
    # `--bind-addr 127.0.0.1:<port> --no-tls` on the top-level daemon
    # path.  `serve-local` is the SSOT for the trusted-local-backend
    # invariant (its handler in nexus-vfs profiles/cluster/src/lib.rs
    # forces both), and its docstring explicitly calls out that it
    # exists so this invariant lives in the binary instead of being
    # hand-written — and drifting — at each spawn site (the
    # `--bootstrap-mode` breakage that hit sudowork / moss / sudocode
    # at once is the failure mode this closes).  This script was one
    # of those drift sites; the fix routes it back through the SSOT.
    #
    # `--plugin-dir` and `--data-dir` are `global = true` clap args on
    # the daemon's CommonArgs, so they compose with the subcommand.
    proc = subprocess.Popen(
        [
            str(nexusd_cluster),
            "serve-local",
            "--port",
            str(port),
            "--plugin-dir",
            str(plugin_dir),
            "--data-dir",
            str(data_dir),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_port(port, deadline=time.monotonic() + CLUSTER_READY_TIMEOUT_S)
    except SystemExit:
        kill_cluster(proc)
        raise
    return proc, port


def kill_cluster(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            proc.kill()
        else:
            proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)


def load_keystore(keys_json: Path) -> dict[str, dict[str, str]]:
    if not keys_json.is_file():
        raise SystemExit(
            f"keystore missing: {keys_json}\nRun scripts/dogfood_keystore_init.py first."
        )
    return json.loads(keys_json.read_text(encoding="ascii"))


def save_keystore(keys_json: Path, store: dict[str, dict[str, str]]) -> None:
    text = json.dumps(store, indent=2, sort_keys=True) + "\n"
    keys_json.write_text(text, encoding="ascii")


def rehydrate(stub, secrets_pb2, store: dict[str, dict[str, str]]) -> None:
    """PutSecretSealed every keystore entry so vault state mirrors the
    committed JSON. Stable iteration order is alphabetical (sorted)."""
    for name, entry in sorted(store.items()):
        nonce = base64.b64decode(entry["nonce"])
        ciphertext = base64.b64decode(entry["ciphertext"])
        stub.PutSecretSealed(
            secrets_pb2.PutSecretSealedRequest(
                namespace=entry.get("namespace", SIGNING_NAMESPACE),
                key=name,
                nonce=nonce,
                ciphertext=ciphertext,
                description=entry.get("description", ""),
            )
        )


def import_proto_module(tempdir: Path) -> tuple:
    """Compile `secrets.proto` into `tempdir` and import the resulting
    `secrets_pb2` + `secrets_pb2_grpc` modules. Returns the tuple
    `(secrets_pb2, secrets_pb2_grpc)`.
    """
    compile_proto_into(tempdir)
    sys.path.insert(0, str(tempdir))
    from nexus.secrets.v1 import secrets_pb2, secrets_pb2_grpc

    return secrets_pb2, secrets_pb2_grpc
