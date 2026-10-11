"""Create and clean up VFS facts for the native TypeScript Search E2E suite."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from nexus.remote.rpc_transport import RPCTransport


def main(action: str) -> None:
    destination = Path(os.environ["NEXUS_TS_SEARCH_FIXTURE"])
    admin = Path(os.environ["NEXUS_SEARCH_TEST_ADMIN_KEY"]).read_text().strip()
    base = os.environ["NEXUS_SEARCH_TEST_HTTP"].rstrip("/")
    tls = SimpleNamespace(
        ca_pem=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CA"]).read_bytes(),
        node_cert_pem=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CERT"]).read_bytes(),
        node_key_pem=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_KEY"]).read_bytes(),
    )
    transport = RPCTransport(
        os.environ["NEXUS_SEARCH_PLUGIN_TARGET"], auth_token=admin, tls_config=tls, timeout=30
    )

    def http(method: str, route: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"{base}{route}",
            method=method,
            headers={"Authorization": f"Bearer {admin}", "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None,
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        if result.get("error"):
            raise RuntimeError(f"Fixture {route} failed: {result['error']}")
        return result

    def cleanup(manifest: dict) -> None:
        if manifest.get("grant"):
            http("DELETE", "/v2/rebac/tuples", manifest["grant"])
        if manifest.get("keyHash"):
            http("DELETE", f"/v2/auth/keys/{manifest['keyHash']}")
        for path in manifest["created"]:
            transport.delete_file(path)

    try:
        if action == "cleanup":
            if destination.exists():
                cleanup(json.loads(destination.read_text()))
                destination.unlink()
            return
        if action != "prepare":
            raise ValueError("action must be prepare or cleanup")
        suffix = uuid4().hex
        root = f"/docs/ts-search-{suffix}"
        manifest = {
            "url": base,
            "adminKey": admin,
            "root": root,
            "visible": f"{root}/visible.txt",
            "hidden": f"{root}/hidden.txt",
            "needle": f"marigold{suffix[:8]}",
            "created": [],
        }
        try:
            subject = f"ts-reader-{suffix}"
            minted = http(
                "POST",
                "/v2/auth/keys",
                {"subject_type": "user", "subject_id": subject, "zones": ["sharedzone:r"]},
            )
            manifest["readerKey"] = minted["key"]
            manifest["keyHash"] = minted["key_hash"]
            text = f"before\n{manifest['needle']} native search\nafter\n"
            documents = []
            for field in ("visible", "hidden"):
                path = manifest[field]
                transport.write_file(path, text.encode())
                manifest["created"].append(path)
                documents.append({"path": path, "text": text})
            indexed = http(
                "POST", "/v2/documents/batch", {"documents": documents, "zone_id": "sharedzone"}
            )
            assert indexed["indexed_count"] == len(documents)
            manifest["grant"] = {
                "zone": "sharedzone",
                "object_type": "file",
                "object_id": manifest["visible"],
                "relation": "viewer",
                "subject_type": "user",
                "subject_id": subject,
            }
            http("POST", "/v2/rebac/tuples", manifest["grant"])
            destination.write_text(json.dumps(manifest), encoding="utf-8")
            destination.chmod(0o600)
        except BaseException:
            cleanup(manifest)
            raise
        print("Native TypeScript Search fixture prepared", flush=True)
    finally:
        transport.close()


if __name__ == "__main__":
    main(sys.argv[1])
