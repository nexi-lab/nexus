#!/usr/bin/env python3
"""Add a new Ed25519 signing keypair to the sealed keystore.

Operator-run script. Boots an ephemeral nexusd-cluster + vault.dylib
on localhost using the master key from `$VAULT_SIGNING_MASTER_KEY`,
rehydrates the existing committed keystore via `PutSecretSealed`,
generates a fresh Ed25519 keypair, stores the privkey via `PutSecret`
(vault seals it under the master key), captures the sealed form via
`GetSecretSealed`, writes it back into the keystore JSON for commit,
writes the pubkey alongside, and round-trips a sign/verify self-test
before tearing the cluster down.

Workflow for a new plugin's signing root:

  1. Have `nexusd-cluster` binary + `nexus-vault` cdylib available;
     either pass them via `--nexusd-cluster` / `--vault-dylib` or
     have them on `$PATH` / under the cargo target dir.
  2. `export VAULT_SIGNING_MASTER_KEY=<base64-32-bytes>`
  3. `python scripts/provision_dogfood_key.py --key-name kernel-dogfood-v1`
  4. Commit `data/vault-signing/keys.json` +
     `data/vault-signing/pubkeys/<key-name>.pub`.
  5. Cross-repo: append `<key-name>.pub` to nexus-vfs's
     `TRUSTED_KEY_FILES` so the cluster's plugin loader trusts the
     signature next build.

Re-running with the same key name appends a new version into vault
but the keystore JSON ALWAYS holds the latest version's sealed bytes
(deterministic diff for review). Rotation = mint a new key name; the
old pubkey + sealed entry stays in the keystore until the cross-repo
trust-root drop is coordinated.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROTO_REL = Path("rust/services/proto/nexus/secrets/v1/secrets.proto")
PROTO_IMPORT_ROOT_REL = Path("rust/services/proto")
DEFAULT_KEYSTORE_REL = Path("data/vault-signing")

PRIVKEY_LENGTH = 32
PUBKEY_LENGTH = 32
SIGNATURE_LENGTH = 64
MASTER_KEY_BYTES = 32
NAMESPACE = "signing-keys"

CLUSTER_READY_TIMEOUT_S = 30.0
CLUSTER_PORT_POLL_INTERVAL_S = 0.25


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    return here.parent.parent


def _compile_proto_into(out_dir: Path) -> None:
    import grpc_tools
    from grpc_tools import protoc

    proto_root = _repo_root() / PROTO_IMPORT_ROOT_REL
    proto_file = _repo_root() / PROTO_REL
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


def _shred_bytes(buf: bytes | bytearray) -> None:
    if not buf:
        return
    addr = ctypes.cast(ctypes.c_char_p(bytes(buf)), ctypes.c_void_p).value
    if addr:
        ctypes.memset(addr, 0, len(buf))


def _read_master_key_from_env() -> bytes:
    raw = os.environ.get("VAULT_SIGNING_MASTER_KEY", "").strip()
    if not raw:
        raise SystemExit("VAULT_SIGNING_MASTER_KEY env var is required")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise SystemExit(f"VAULT_SIGNING_MASTER_KEY is not valid base64: {exc}") from exc
    if len(decoded) != MASTER_KEY_BYTES:
        raise SystemExit(
            f"VAULT_SIGNING_MASTER_KEY decodes to {len(decoded)} bytes; expected {MASTER_KEY_BYTES}"
        )
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


def _default_nexusd_cluster() -> Path | None:
    return _find_default_path(
        "NEXUSD_CLUSTER",
        [Path("nexusd-cluster.exe"), Path("nexusd-cluster")],
    )


def _default_vault_dylib() -> Path | None:
    name_candidates = [
        "libnexus_vault.so",
        "libnexus_vault.dylib",
        "nexus_vault.dll",
    ]
    target = _repo_root() / "target"
    candidates = [
        target / profile / name for profile in ("release", "debug") for name in name_candidates
    ]
    return _find_default_path("VAULT_DYLIB_PATH", candidates)


def _free_port() -> int:
    """Reserve an OS-assigned port. The chosen port races with cluster
    binding but is the simplest portable way to pre-allocate one."""
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


def _boot_cluster(
    *,
    nexusd_cluster: Path,
    vault_dylib: Path,
    master_key: bytes,
    data_dir: Path,
) -> tuple[subprocess.Popen[bytes], int]:
    vault_data = data_dir / "vault"
    vault_data.mkdir(parents=True, exist_ok=True)
    (vault_data / "master.key").write_bytes(master_key)

    plugin_dir = data_dir / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    target_dylib = plugin_dir / vault_dylib.name
    target_dylib.write_bytes(vault_dylib.read_bytes())

    port = _free_port()
    env = os.environ.copy()
    env["NEXUS_DATA_DIR"] = str(data_dir)
    env["RUST_LOG"] = env.get("RUST_LOG", "warn")

    proc = subprocess.Popen(
        [
            str(nexusd_cluster),
            "--plugin-dir",
            str(plugin_dir),
            "--no-tls",
            "--bind",
            f"127.0.0.1:{port}",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    _wait_for_port(port, deadline=time.monotonic() + CLUSTER_READY_TIMEOUT_S)
    return proc, port


def _kill_cluster(proc: subprocess.Popen[bytes]) -> None:
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


def _load_keystore(keys_json: Path) -> dict[str, dict[str, str]]:
    if not keys_json.is_file():
        raise SystemExit(
            f"keystore missing: {keys_json}\nRun scripts/dogfood_keystore_init.py first."
        )
    return json.loads(keys_json.read_text(encoding="ascii"))


def _save_keystore(keys_json: Path, store: dict[str, dict[str, str]]) -> None:
    text = json.dumps(store, indent=2, sort_keys=True) + "\n"
    keys_json.write_text(text, encoding="ascii")


def _rehydrate(stub, secrets_pb2, store: dict[str, dict[str, str]]) -> None:
    for name, entry in sorted(store.items()):
        nonce = base64.b64decode(entry["nonce"])
        ciphertext = base64.b64decode(entry["ciphertext"])
        req = secrets_pb2.PutSecretSealedRequest(
            namespace=entry.get("namespace", NAMESPACE),
            key=name,
            nonce=nonce,
            ciphertext=ciphertext,
            description=entry.get("description", ""),
        )
        stub.PutSecretSealed(req)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Add a new Ed25519 signing keypair to the sealed keystore."
    )
    p.add_argument(
        "--key-name",
        required=True,
        help="Signing key identity (e.g. kernel-dogfood-v1). Becomes the pubkey filename and the keystore JSON key.",
    )
    p.add_argument(
        "--description",
        default="",
        help="Optional human-readable description stored in the keystore JSON.",
    )
    p.add_argument(
        "--keystore-dir",
        type=Path,
        default=_repo_root() / DEFAULT_KEYSTORE_REL,
        help="Directory holding keys.json + pubkeys/.",
    )
    p.add_argument(
        "--nexusd-cluster",
        type=Path,
        default=_default_nexusd_cluster(),
        help=(
            "Path to nexusd-cluster binary. Falls back to $NEXUSD_CLUSTER, then "
            "`nexusd-cluster`/`nexusd-cluster.exe` on $PATH."
        ),
    )
    p.add_argument(
        "--vault-dylib",
        type=Path,
        default=_default_vault_dylib(),
        help=(
            "Path to the nexus-vault cdylib. Falls back to $VAULT_DYLIB_PATH, "
            "then target/{release,debug}/libnexus_vault.*."
        ),
    )
    args = p.parse_args(argv)

    if args.nexusd_cluster is None or not Path(args.nexusd_cluster).is_file():
        raise SystemExit(
            "nexusd-cluster binary not found; pass --nexusd-cluster or set $NEXUSD_CLUSTER"
        )
    if args.vault_dylib is None or not Path(args.vault_dylib).is_file():
        raise SystemExit(
            "nexus-vault cdylib not found; pass --vault-dylib or set $VAULT_DYLIB_PATH"
        )

    keystore_dir: Path = args.keystore_dir
    keys_json = keystore_dir / "keys.json"
    pubkeys_dir = keystore_dir / "pubkeys"
    pub_path = pubkeys_dir / f"{args.key_name}.pub"
    if pub_path.exists():
        raise SystemExit(f"refusing to overwrite existing pubkey: {pub_path}")
    pubkeys_dir.mkdir(parents=True, exist_ok=True)

    master_key = _read_master_key_from_env()
    store = _load_keystore(keys_json)
    if args.key_name in store:
        raise SystemExit(
            f"keystore already has an entry named {args.key_name!r}; rotate by minting a new name"
        )

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    privkey = Ed25519PrivateKey.generate()
    raw_priv: bytes = privkey.private_bytes_raw()
    raw_pub: bytes = privkey.public_key().public_bytes_raw()
    assert len(raw_priv) == PRIVKEY_LENGTH
    assert len(raw_pub) == PUBKEY_LENGTH
    b64_priv = base64.b64encode(raw_priv).decode("ascii")
    b64_pub = base64.b64encode(raw_pub).decode("ascii")

    with tempfile.TemporaryDirectory(prefix="provision-dogfood-key-") as td:
        td_path = Path(td)
        proto_dir = td_path / "proto"
        proto_dir.mkdir()
        _compile_proto_into(proto_dir)
        sys.path.insert(0, str(proto_dir))

        import grpc

        from nexus.secrets.v1 import secrets_pb2, secrets_pb2_grpc

        cluster_data = td_path / "cluster"
        proc, port = _boot_cluster(
            nexusd_cluster=args.nexusd_cluster,
            vault_dylib=args.vault_dylib,
            master_key=master_key,
            data_dir=cluster_data,
        )
        try:
            channel = grpc.insecure_channel(f"127.0.0.1:{port}")
            stub = secrets_pb2_grpc.GenericSecretsServiceStub(channel)

            _rehydrate(stub, secrets_pb2, store)

            stub.PutSecret(
                secrets_pb2.PutSecretRequest(
                    namespace=NAMESPACE,
                    key=args.key_name,
                    value=b64_priv,
                    description=args.description or f"plugin signing privkey ({args.key_name})",
                )
            )

            sealed = stub.GetSecretSealed(
                secrets_pb2.GetSecretRequest(namespace=NAMESPACE, key=args.key_name)
            )

            store[args.key_name] = {
                "namespace": NAMESPACE,
                "nonce": base64.b64encode(sealed.nonce).decode("ascii"),
                "ciphertext": base64.b64encode(sealed.ciphertext).decode("ascii"),
                "description": args.description,
            }
            _save_keystore(keys_json, store)
            pub_path.write_text(b64_pub + "\n", encoding="ascii")

            sample = b"plugin-signing-dogfood-self-test"
            sig = privkey.sign(sample)
            if len(sig) != SIGNATURE_LENGTH:
                raise SystemExit(f"unexpected signature length {len(sig)}")
            try:
                Ed25519PublicKey.from_public_bytes(raw_pub).verify(sig, sample)
            except InvalidSignature as exc:
                raise SystemExit("self-test: pubkey did not verify the privkey's sig") from exc
        finally:
            _kill_cluster(proc)

    _shred_bytes(raw_priv)
    _shred_bytes(master_key)

    print(
        f"provisioned: {args.key_name}\n"
        f"  keystore: {keys_json}\n"
        f"  pubkey:   {pub_path}\n"
        f"pubkey (base64): {b64_pub}\n\n"
        "Next steps:\n"
        f"  1. git add {keys_json.relative_to(_repo_root())} {pub_path.relative_to(_repo_root())}\n"
        f"  2. Commit + open PR (CODEOWNERS will request kernel-team review).\n"
        f"  3. In nexus-vfs, drop the same pubkey under rust/kernel/trusted_keys/\n"
        f"     and add an include_bytes! entry to TRUSTED_KEY_FILES — merge that\n"
        f"     BEFORE the nexus pubkey lands so cluster builds trust the new root.\n",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
