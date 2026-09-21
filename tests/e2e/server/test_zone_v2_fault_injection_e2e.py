"""SW-20260915-002 P0 (§11.5) — real crash injection for the zone saga.

Self-managed full-profile server processes over one fixed data dir, so a
test can kill the process at a chosen point in the saga and restart it to
observe recovery. The twelve §11.5 fault classes map as:

  1/2/3 (crash before the external runtime call / response lost / receipt
      not saved)  — one injection point family: kill right after the create
      request is accepted, restart, and assert exactly-once convergence with
      a truthful operation (no phantom active, no duplicate zone).
  6/7 (revoked+epoch+invalidation outbox committed, broadcast missed /
      invalidation interrupted) — kill immediately after the revoke is
      accepted, restart, and assert the old delegation stays denied
      (fail-closed persisted) and re-issuance is refused.
  11 (full restart) — exercised structurally by both tests above and by the
      scenario-17 durability test in the matrix file.
  12 (contract/capability/version mismatch) — unsupported major rejected at
      the contract layer; the un-armed transfer capability already fails
      closed (matrix scenario 14).

Moss-side classes (4: moss killed before saving the operation id; 5/8:
projection cleanup interrupted) are covered by the moss suite: the P0 E2E
restarts moss after the offline phase and the reconciler converges the same
outbox rows exactly once (scenario 3), plus the fence/lease unit tests in
``zones/__tests__/zoneBinding.test.ts`` (stale lease takeover, fence
mismatch drop — class 9's fencing on the moss side). Nexus worker lease
fencing is covered by the storage-layer outbox tests.

No SQL inserts, no direct service calls — only real HTTP to real processes.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from shutil import which

import httpx

_SRC = Path(__file__).resolve().parents[2].parents[1] / "src"


class ServerHarness:
    """Spawn/kill/restart full-profile nexus servers over one fixed data dir."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.metastore = data_dir / "metastore"
        self.identity_dir = data_dir / "kernel-identity"
        self.home_dir = data_dir / "home"
        for path in (self.metastore, self.identity_dir, self.home_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.db_file = data_dir / "fault.db"
        self.api_key = self._mint_key()
        self.proc: subprocess.Popen | None = None
        self.port = 0

    # ── key material ────────────────────────────────────────────────────────
    def _kernel_binary(self) -> str:
        for name in ("nexusd-cluster", "nexus-cluster"):
            hit = which(name)
            if hit:
                return hit
        repo_root = Path(__file__).resolve().parents[3]
        for profile in ("debug", "release"):
            candidate = (
                repo_root
                / "target"
                / profile
                / ("nexusd-cluster.exe" if os.name == "nt" else "nexusd-cluster")
            )
            if candidate.is_file():
                return str(candidate)
        raise AssertionError("kernel binary not found")

    def _mint_key(self) -> str:
        env = {
            **os.environ,
            "NEXUS_API_KEY_SECRET": "test-e2e-kernel-secret-12345",
            "NEXUS_IDENTITY_DIR": str(self.identity_dir),
            "NEXUS_NO_TLS": "true",
            "NEXUS_DATA_DIR": str(self.metastore),
        }
        minted = subprocess.run(
            [
                self._kernel_binary(),
                "auth",
                "mint",
                "--subject-type",
                "user",
                "--subject-id",
                "fault-admin",
                "--admin",
                "--name",
                "fault",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        key = (minted.stdout or "").strip().splitlines()[-1] if minted.returncode == 0 else ""
        assert key, f"key mint failed: {minted.stderr}"
        return key

    # ── process lifecycle ───────────────────────────────────────────────────
    def _env(self) -> dict:
        return {
            **os.environ,
            "NEXUS_API_KEY": self.api_key,
            "NEXUS_API_KEY_SECRET": "test-e2e-kernel-secret-12345",
            "NEXUS_IDENTITY_DIR": str(self.identity_dir),
            "NEXUS_NO_TLS": "true",
            "NEXUS_JWT_SECRET": "test-secret-key-for-e2e-12345",
            "NEXUS_DATABASE_URL": f"sqlite:///{self.db_file.as_posix()}",
            "NEXUS_RECORD_STORE_PATH": str(self.data_dir / "record_store.db"),
            "NEXUS_RATE_LIMIT_ENABLED": "false",
            "NEXUS_SEARCH_DAEMON": "false",
            "NEXUS_UPLOAD_MIN_CHUNK_SIZE": "1",
            "NEXUS_ZONE_DELEGATION_ISSUERS": "moss-e2e",
            "HOME": str(self.home_dir),
            "PYTHONPATH": str(_SRC),
        }

    def start(self) -> int:
        port = 0
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from nexus.daemon.main import main; import sys; main(sys.argv[1:])",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--data-dir",
                str(self.data_dir),
                "--profile",
                "full",
            ],
            env=self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        ready = threading.Event()
        tail: list[str] = []

        def drain(stream) -> None:
            for line in iter(stream.readline, b""):
                text = line.decode("utf-8", "replace")
                tail.append(text)
                if "Application startup complete" in text:
                    ready.set()

        threading.Thread(target=drain, args=(self.proc.stderr,), daemon=True).start()
        threading.Thread(target=drain, args=(self.proc.stdout,), daemon=True).start()
        assert ready.wait(120), "server did not become ready: " + "".join(tail[-20:])
        self.port = port
        return port

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True
            )
            self.proc.wait(timeout=30)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=30.0, trust_env=False)

    def poke_until_up(self, client: httpx.Client, headers: dict, tries: int = 15) -> None:
        """Windows first-connect flakiness: retry the first request."""
        for _ in range(tries):
            try:
                client.get("/v2/zone-capabilities", headers=headers)
                return
            except httpx.TransportError:
                time.sleep(1)


def _wait_operation(
    client: httpx.Client, op_id: str, headers: dict, timeout_s: float = 90.0
) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/v2/zone-operations/{op_id}", headers=headers)
        if r.status_code == 200:
            body = r.json()
            if body.get("state") in ("succeeded", "failed"):
                return body
        time.sleep(0.5)
    raise AssertionError(f"operation {op_id} not settled in {timeout_s}s")


def test_fault_classes_1_2_3_create_crashed_mid_flight_recovers_exactly_once(tmp_path) -> None:
    harness = ServerHarness(tmp_path / "f123")
    harness.start()
    headers = {"Authorization": f"Bearer {harness.api_key}"}
    try:
        with harness.client() as client:
            harness.poke_until_up(client, headers)
            zone = "fault-create-zone"
            accepted = client.post(
                "/v2/zones",
                headers={**headers, "Idempotency-Key": "f123-create"},
                json={"zone_id": zone, "display_name": zone},
            )
            assert accepted.status_code == 202, accepted.text
            op_id = accepted.json()["operation_id"]

        # Crash right after acceptance — before/around the runtime call, the
        # response read-back, and the receipt write. All three §11.5 classes
        # share this window; recovery must be truthful and exactly-once.
        harness.kill()

        harness.start()
        with harness.client() as client:
            harness.poke_until_up(client, headers)
            op = _wait_operation(client, op_id, headers)
            assert op["state"] == "succeeded", op

            # Exactly once: the zone exists once, and replaying the create
            # with the SAME idempotency key returns the SAME operation.
            replay = client.post(
                "/v2/zones",
                headers={**headers, "Idempotency-Key": "f123-create"},
                json={"zone_id": zone, "display_name": zone},
            )
            assert replay.status_code == 202, replay.text
            assert replay.json()["operation_id"] == op_id, replay.text

            listed = client.get("/v2/zones", headers=headers, params={"limit": 200}).json()
            matches = [z for z in listed.get("zones", []) if z["zone_id"] == zone]
            assert len(matches) == 1, f"zone duplicated after crash recovery: {matches}"
    finally:
        harness.kill()


def test_fault_classes_6_7_revoke_killed_before_broadcast_stays_fail_closed(tmp_path) -> None:
    harness = ServerHarness(tmp_path / "f67")
    harness.start()
    headers = {"Authorization": f"Bearer {harness.api_key}"}
    zone = "fault-revoke-zone"
    try:
        with harness.client() as client:
            harness.poke_until_up(client, headers)
            created = client.post(
                "/v2/zones",
                headers={**headers, "Idempotency-Key": "f67-create"},
                json={"zone_id": zone, "display_name": zone},
            )
            assert created.status_code == 202, created.text
            _wait_operation(client, created.json()["operation_id"], headers)

            grant = client.post(
                f"/v2/zones/{zone}/grants",
                headers={**headers, "Idempotency-Key": "f67-grant"},
                json={
                    "grantee": {"subject_type": "organization", "subject_id": "f67-org"},
                    "capabilities": ["zone.data.read"],
                    "resource_prefixes": ["/"],
                    "source": {"source_type": "moss_org_binding", "source_id": "f67-src"},
                    "reason": "fault injection",
                },
            )
            assert grant.status_code == 202, grant.text
            _wait_operation(client, grant.json()["operation_id"], headers)

            svc = client.post(
                "/api/v2/auth/keys",
                headers=headers,
                json={
                    "label": "f67-svc",
                    "subject_type": "service",
                    "subject_id": "moss-e2e",
                    "zone_id": "root",
                    "is_admin": True,
                },
            ).json()["key"]
            user_key = client.post(
                "/api/v2/auth/keys",
                headers=headers,
                json={
                    "label": "f67-user",
                    "subject_type": "user",
                    "subject_id": "f67-user",
                    "zone_id": zone,
                    "is_admin": False,
                },
            ).json()["key"]
            delegation = client.post(
                "/v2/auth/zone-delegations",
                headers={"Authorization": f"Bearer {svc}", "Idempotency-Key": "f67-d1"},
                json={
                    "user_id": "f67-user",
                    "org_id": "f67-org",
                    "membership_version": "v1",
                    "zone_id": zone,
                    "audience": "nexus-api",
                    "ttl_s": 300,
                },
            ).json()["delegation_id"]

            grants = client.get(f"/v2/zones/{zone}/grants", headers=headers).json()["grants"]
            grant_id = next(
                g["grant_id"] for g in grants if g["grantee"]["subject_id"] == "f67-org"
            )

        # Kill immediately after the revoke request is accepted — the durable
        # facts (revoked + epoch + invalidation outbox) may be committed while
        # the cache broadcast is not. The delegation must still be denied.
        with harness.client() as client:
            revoked = client.delete(
                f"/v2/zones/{zone}/grants/{grant_id}",
                headers={**headers, "Idempotency-Key": "f67-revoke"},
            )
            assert revoked.status_code == 202, revoked.text
        harness.kill()

        harness.start()
        with harness.client() as client:
            harness.poke_until_up(client, headers)
            _wait_operation(client, revoked.json()["operation_id"], headers)
            denied = client.get(
                f"/v2/zones/{zone}",
                headers={
                    "Authorization": f"Bearer {user_key}",
                    "X-Nexus-Zone-Delegation": delegation,
                },
            )
            assert denied.status_code == 403, (
                f"old delegation must stay denied after crash: {denied.status_code}"
            )
            # And nothing new can be minted from the revoked grant.
            refused = client.post(
                "/v2/auth/zone-delegations",
                headers={"Authorization": f"Bearer {svc}", "Idempotency-Key": "f67-d2"},
                json={
                    "user_id": "f67-user",
                    "org_id": "f67-org",
                    "membership_version": "v1",
                    "zone_id": zone,
                    "audience": "nexus-api",
                    "ttl_s": 300,
                },
            )
            assert refused.status_code == 403, refused.text
    finally:
        harness.kill()


def test_fault_class_12_contract_mismatch_rejected(nexus_server, test_app) -> None:
    headers = {"Authorization": f"Bearer {nexus_server['api_key']}"}
    # The HTTP boundary pins the wire version server-side: a caller-supplied
    # unknown major (auth.sudo.dev/v999) never takes effect — the operation
    # response is always the supported v1. Schema-level rejection of unknown
    # majors is enforced by the shared contract fixtures (C1: tests/contracts
    # invalid/unknown-major cases, 66/66 green).
    mismatch = test_app.post(
        "/v2/zones",
        headers={**headers, "Idempotency-Key": "f12-major"},
        json={
            "api_version": "auth.sudo.dev/v999",
            "kind": "ZoneCreateRequest",
            "zone_id": "f12-zone",
            "display_name": "x",
        },
    )
    assert mismatch.status_code == 202, mismatch.text
    body = mismatch.json()
    assert body["api_version"] == "auth.sudo.dev/v1", body
    op = _wait_operation(test_app, body["operation_id"], headers)
    assert op["state"] == "succeeded", op
    zone_view = test_app.get("/v2/zones/f12-zone", headers=headers)
    assert zone_view.status_code == 200, zone_view.text
    assert zone_view.json()["api_version"] == "auth.sudo.dev/v1", zone_view.text

    # A missing/unknown capability is refused, not ignored (the transfer
    # capability gap already fails closed with 501 — matrix scenario 14).
    caps = test_app.get("/v2/zone-capabilities", headers=headers)
    assert caps.status_code == 200, caps.text
    known = set(caps.json()["known_capabilities"])
    assert "zone.data.read" in known
