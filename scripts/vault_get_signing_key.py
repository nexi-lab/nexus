#!/usr/bin/env python3
"""Fetch a plugin signing privkey from a running vault cluster.

CI shim for the plugin-signing dogfood path. When a release workflow sets
`signing_key_source: vault`, it runs this script to pull the privkey out
of vault and pipe it into `$GITHUB_ENV` as `PLUGIN_SIGNING_PRIVKEY` so
`sign_plugin.py` can read it from env without ever touching disk.

The other source (`github-secret`) sets `PLUGIN_SIGNING_PRIVKEY` directly
from a repo secret — same env var, same downstream signer.

Env contract:
  VAULT_ENDPOINT   gRPC endpoint of the cluster running vault.dylib
                   (e.g. "vault-signing.internal:2126").
  VAULT_TOKEN      Bearer token for the vault plugin's auth surface.
  VAULT_INSECURE   When "1", use grpc.insecure_channel instead of TLS.
                   Required for the ephemeral-vault-on-runner path (no TLS,
                   localhost only) and the kernel-team local-provisioning
                   workflow. Unset / "0" in any prod / network path.

CLI:
  vault_get_signing_key.py <namespace>/<key>
    e.g. vault_get_signing_key.py signing-keys/kernel-dogfood-v1

Prints the value (already base64 of 32 raw Ed25519 priv bytes — same
format `sign_plugin.py` expects) to stdout with no trailing newline.
Refuses to run if either env var is unset.

Proto stubs are compiled into a tempdir at runtime via `grpc_tools.protoc`
rather than committed in-tree — this script is the only Python consumer
of `nexus.secrets.v1` and CI installs `grpcio-tools` for one job per
release.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ENDPOINT_ENV = "VAULT_ENDPOINT"
TOKEN_ENV = "VAULT_TOKEN"
INSECURE_ENV = "VAULT_INSECURE"

# Repo-relative path to the secrets proto SSOT.
PROTO_REL = Path("rust/services/proto/nexus/secrets/v1/secrets.proto")
PROTO_IMPORT_ROOT_REL = Path("rust/services/proto")


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(f"environment variable {name} is empty or unset")
    return val


def _parse_key(arg: str) -> tuple[str, str]:
    if "/" not in arg:
        raise SystemExit(
            f"expected NAMESPACE/KEY (e.g. signing-keys/kernel-dogfood-v1), got: {arg!r}"
        )
    namespace, key = arg.split("/", 1)
    if not namespace or not key:
        raise SystemExit(f"namespace and key must both be non-empty: {arg!r}")
    return namespace, key


def _repo_root() -> Path:
    # scripts/vault_get_signing_key.py → repo root is parent of scripts/.
    here = Path(__file__).resolve()
    return here.parent.parent


def _compile_proto_into(out_dir: Path) -> None:
    """Generate `*_pb2.py` + `*_pb2_grpc.py` for `secrets.proto` into out_dir."""
    from grpc_tools import protoc

    proto_root = _repo_root() / PROTO_IMPORT_ROOT_REL
    proto_file = _repo_root() / PROTO_REL
    if not proto_file.is_file():
        raise SystemExit(f"proto SSOT missing: {proto_file}")

    rc = protoc.main(
        [
            "grpc_tools.protoc",
            f"--proto_path={proto_root}",
            f"--python_out={out_dir}",
            f"--grpc_python_out={out_dir}",
            str(proto_file),
        ]
    )
    if rc != 0:
        raise SystemExit(f"protoc exited {rc} when compiling {proto_file}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    if len(argv) != 1:
        raise SystemExit("expected exactly one positional argument: NAMESPACE/KEY")

    namespace, key = _parse_key(argv[0])
    endpoint = _require_env(ENDPOINT_ENV)
    token = _require_env(TOKEN_ENV)
    insecure = os.environ.get(INSECURE_ENV, "").strip() == "1"

    with tempfile.TemporaryDirectory(prefix="vault-get-signing-key-") as td:
        out_dir = Path(td)
        _compile_proto_into(out_dir)
        sys.path.insert(0, str(out_dir))

        import grpc

        from nexus.secrets.v1 import secrets_pb2, secrets_pb2_grpc

        if insecure:
            channel = grpc.insecure_channel(endpoint)
        else:
            channel = grpc.secure_channel(endpoint, grpc.ssl_channel_credentials())
        stub = secrets_pb2_grpc.GenericSecretsServiceStub(channel)
        req = secrets_pb2.GetSecretRequest(namespace=namespace, key=key)
        metadata = (("authorization", f"Bearer {token}"),)
        resp = stub.GetSecret(req, metadata=metadata)

        # Stdout only, no newline — workflow appends to `$GITHUB_ENV` as
        # `PLUGIN_SIGNING_PRIVKEY=$(python ...)` and a trailing \n would
        # corrupt the parsed value.
        sys.stdout.write(resp.value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
