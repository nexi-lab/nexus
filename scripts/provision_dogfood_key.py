#!/usr/bin/env python3
"""One-shot ops tool to provision a plugin signing keypair into vault.

Generates an Ed25519 keypair in memory, stores the privkey in vault under
`<namespace>/<key>` (default namespace: `signing-keys`), writes the pubkey
to `./<key-name>.pub` for manual commit into a kernel trust root, and
self-tests the round trip: vault GetSecret → sign sample → verify against
pubkey. The in-memory privkey is shredded before exit.

Workflow for a new plugin's signing root:
  1. Run this script against the dogfood vault cluster.
  2. Commit the generated `<key-name>.pub` into
     `nexus-vfs/rust/kernel/trusted_keys/<key-name>.pub`.
  3. Append the `include_bytes!` line to `TRUSTED_KEY_FILES` in
     `nexus-vfs/rust/kernel/src/kernel/plugins/loader.rs`.
  4. Next cluster build embeds the trust root.
  5. Future plugin release CI fetches the privkey via
     scripts/vault_get_signing_key.py.

Run only once per signing-key identity. Re-running with the same key name
would create a NEW version in vault — the old privkey is still recoverable
via GetSecret(version=…). Always rotate by provisioning a fresh
`<key-name>-v<n+1>` and then deprecating the old one.

Self-contains proto compilation via grpc_tools.protoc (same pattern as
scripts/vault_get_signing_key.py — no committed `_pb2` stubs).
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import os
import sys
import tempfile
from pathlib import Path

# Repo-relative path to the secrets proto SSOT.
PROTO_REL = Path("rust/services/proto/nexus/secrets/v1/secrets.proto")
PROTO_IMPORT_ROOT_REL = Path("rust/services/proto")

# Pinned format constants — must match `nexus-plugin-abi::signing` /
# scripts/sign_plugin.py. If you bump any of these, bump them in lockstep
# across the kernel trust root, the signer, this provisioner, and the
# downstream `vault_get_signing_key.py` shim.
PRIVKEY_LENGTH = 32
PUBKEY_LENGTH = 32
SIGNATURE_LENGTH = 64


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    return here.parent.parent


def _compile_proto_into(out_dir: Path) -> None:
    import os

    import grpc_tools
    from grpc_tools import protoc

    proto_root = _repo_root() / PROTO_IMPORT_ROOT_REL
    proto_file = _repo_root() / PROTO_REL
    if not proto_file.is_file():
        raise SystemExit(f"proto SSOT missing: {proto_file}")

    # grpc_tools bundles the well-known google protos under _proto/; without
    # passing this path, secrets.proto's `import "google/protobuf/timestamp.proto"`
    # fails at compile time.
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


def _shred_bytes(buf: bytes) -> None:
    """Best-effort in-place zero of a bytes buffer's backing memory.

    Python doesn't guarantee strings are unique allocations, but for
    privkey material that lives only as a local `bytes` we have a fresh
    allocation. ctypes.memset on the buffer address overwrites it before
    GC. Not bulletproof against forensic memory dumps, but reduces the
    window the live privkey sits in process RSS.
    """
    if not buf:
        return
    addr = ctypes.cast(ctypes.c_char_p(buf), ctypes.c_void_p).value
    if addr:
        ctypes.memset(addr, 0, len(buf))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Provision a plugin signing keypair into vault.",
    )
    p.add_argument(
        "--vault-endpoint",
        required=True,
        help="gRPC endpoint of the cluster running vault.dylib (e.g. vault-signing.internal:2126).",
    )
    p.add_argument(
        "--vault-token",
        default="",
        help="Admin bearer token for the vault plugin's auth surface. Required unless --insecure.",
    )
    p.add_argument(
        "--insecure",
        action="store_true",
        default=os.environ.get("VAULT_INSECURE", "").strip() == "1",
        help=(
            "Connect without TLS and skip the bearer token. Use only for ephemeral "
            "localhost vault instances (sealed-keystore dogfood flow). Equivalent to "
            "VAULT_INSECURE=1 in env."
        ),
    )
    p.add_argument(
        "--key-name",
        required=True,
        help="Signing key identity (e.g. kernel-dogfood-v1). Also the filename of the generated .pub file.",
    )
    p.add_argument(
        "--namespace",
        default="signing-keys",
        help="Vault namespace to store the privkey under (default: signing-keys).",
    )
    p.add_argument(
        "--pubkey-out-dir",
        type=Path,
        default=Path.cwd(),
        help="Directory to write <key-name>.pub into (default: cwd).",
    )
    p.add_argument(
        "--description",
        default="",
        help="Optional description recorded with the vault secret metadata.",
    )
    args = p.parse_args(argv)

    if not args.insecure and not args.vault_token:
        raise SystemExit("--vault-token is required unless --insecure is set")

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    privkey = Ed25519PrivateKey.generate()
    raw_priv: bytes = privkey.private_bytes_raw()
    raw_pub: bytes = privkey.public_key().public_bytes_raw()
    assert len(raw_priv) == PRIVKEY_LENGTH, f"unexpected priv length {len(raw_priv)}"
    assert len(raw_pub) == PUBKEY_LENGTH, f"unexpected pub length {len(raw_pub)}"

    b64_priv = base64.b64encode(raw_priv).decode("ascii")
    b64_pub = base64.b64encode(raw_pub).decode("ascii")

    pub_path = args.pubkey_out_dir / f"{args.key_name}.pub"
    if pub_path.exists():
        raise SystemExit(f"refusing to overwrite existing pubkey file: {pub_path}")
    pub_path.write_text(b64_pub + "\n", encoding="ascii")
    print(f"wrote pubkey: {pub_path}", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="provision-dogfood-key-") as td:
        out_dir = Path(td)
        _compile_proto_into(out_dir)
        sys.path.insert(0, str(out_dir))

        import grpc

        from nexus.secrets.v1 import secrets_pb2, secrets_pb2_grpc

        if args.insecure:
            channel = grpc.insecure_channel(args.vault_endpoint)
            metadata: tuple[tuple[str, str], ...] = ()
        else:
            channel = grpc.secure_channel(args.vault_endpoint, grpc.ssl_channel_credentials())
            metadata = (("authorization", f"Bearer {args.vault_token}"),)
        stub = secrets_pb2_grpc.GenericSecretsServiceStub(channel)

        # Store.
        put_req = secrets_pb2.PutSecretRequest(
            namespace=args.namespace,
            key=args.key_name,
            value=b64_priv,
            description=args.description or f"plugin signing privkey ({args.key_name})",
        )
        stub.PutSecret(put_req, metadata=metadata)
        print(
            f"stored privkey: vault://{args.vault_endpoint}/{args.namespace}/{args.key_name}",
            file=sys.stderr,
        )

        # Round-trip — fetch back, sign a fixed sample, verify against the
        # in-memory pubkey. Confirms storage didn't truncate / re-encode
        # the value and that GetSecret returns it byte-identical.
        get_req = secrets_pb2.GetSecretRequest(
            namespace=args.namespace,
            key=args.key_name,
        )
        get_resp = stub.GetSecret(get_req, metadata=metadata)
        if get_resp.value != b64_priv:
            raise SystemExit(
                "round-trip mismatch: GetSecret returned a different value than PutSecret stored"
            )

        fetched_raw = base64.b64decode(get_resp.value, validate=True)
        fetched_privkey = Ed25519PrivateKey.from_private_bytes(fetched_raw)
        sample = b"plugin-signing-dogfood-self-test"
        sig = fetched_privkey.sign(sample)
        if len(sig) != SIGNATURE_LENGTH:
            raise SystemExit(f"unexpected signature length {len(sig)}")

        verifier: Ed25519PublicKey = Ed25519PublicKey.from_public_bytes(raw_pub)
        try:
            verifier.verify(sig, sample)
        except InvalidSignature as exc:
            raise SystemExit(
                "self-test failed: pubkey did not verify a sig made by the fetched privkey"
            ) from exc

        _shred_bytes(fetched_raw)
        print("round-trip OK: GetSecret → sign → verify", file=sys.stderr)

    _shred_bytes(raw_priv)
    print(
        f"pubkey (base64): {b64_pub}\n"
        f"next step: commit {pub_path.name} into nexus-vfs/rust/kernel/trusted_keys/ "
        f"and append it to TRUSTED_KEY_FILES.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
