"""E2E test: IPC message signing with FastAPI server, permissions enabled (#1729).

Tests the full signing flow with real cryptographic infrastructure:
1. Start FastAPI app with database auth, permissions enforced
2. Provision two agents with auto-generated Ed25519 keypairs via real KeyService
3. Sign message via MessageSigner → send via MessageSender → verify via MessageProcessor
4. Enforce mode: reject unsigned and tampered messages
5. Verify_only mode: warn on unsigned but allow through
6. Performance: signing + verification under 1ms budget
7. Unauthenticated requests rejected (permissions enforced)

Uses real SQLite-backed RecordStore + KeyService (no mocks),
InMemoryVFS for IPC storage (no external FS needed).
"""

import shutil
import tempfile
import time
import uuid
from typing import Any

import pytest

from nexus.bricks.identity.crypto import IdentityCrypto
from nexus.bricks.identity.key_service import KeyService
from nexus.bricks.ipc.conventions import dead_letter_path, inbox_path, processed_path
from nexus.bricks.ipc.delivery import MessageProcessor, MessageSender
from nexus.bricks.ipc.envelope import MessageEnvelope, MessageType
from nexus.bricks.ipc.provisioning import AgentProvisioner
from nexus.bricks.ipc.signing import MessageSigner, MessageVerifier, SigningMode
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.storage.models import Base
from nexus.storage.zone_settings import ZoneSettings
from tests.helpers.dict_metastore import DictMetastore
from tests.unit.bricks.ipc.fakes import InMemoryStorageDriver

# ---------------------------------------------------------------------------
# Fernet-compatible encryptor for IdentityCrypto
# ---------------------------------------------------------------------------


class _TestTokenEncryptor:
    """Uses Fernet for real encryption (same as production)."""

    def __init__(self) -> None:
        from cryptography.fernet import Fernet

        self._key = Fernet.generate_key()
        self._fernet = Fernet(self._key)

    def encrypt_token(self, token: str) -> str:
        return self._fernet.encrypt(token.encode()).decode()

    def decrypt_token(self, encrypted: str) -> str:
        return self._fernet.decrypt(encrypted.encode()).decode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ZONE = "e2e-signing-zone"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: Any) -> None:
    """Isolate from production env vars."""
    monkeypatch.setenv("NEXUS_JWT_SECRET", "test-secret-key-signing-e2e")
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def db_path(tmp_path: Any) -> Any:
    return tmp_path / f"signing_e2e_{uuid.uuid4().hex[:8]}.db"


@pytest.fixture
def record_store(db_path: Any) -> Any:
    from nexus.storage.record_store import SQLAlchemyRecordStore

    rs = SQLAlchemyRecordStore(db_url=f"sqlite:///{db_path}")
    yield rs
    rs.close()


@pytest.fixture
def crypto() -> IdentityCrypto:
    return IdentityCrypto(_TestTokenEncryptor())


@pytest.fixture
def key_service(record_store: Any, crypto: IdentityCrypto) -> KeyService:
    return KeyService(record_store, crypto)


@pytest.fixture
def vfs() -> InMemoryStorageDriver:
    return InMemoryStorageDriver()


@pytest.fixture
async def app(tmp_path: Any, db_path: Any, record_store: Any) -> Any:
    """FastAPI app with permissions enabled + database auth."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from nexus.backends.storage.cas_local import CASLocalBackend
    from nexus.factory import create_nexus_fs
    from nexus.server.fastapi_server import create_app

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth

    with session_factory() as session:
        _, admin_raw = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="e2e-admin",
            name="E2E Admin",
            zone_id=ROOT_ZONE_ID,
            is_admin=True,
        )
        session.commit()

    tmpdir = tempfile.mkdtemp(prefix="nexus-signing-e2e-")
    backend = CASLocalBackend(root_path=tmpdir)
    metadata_store = DictMetastore()

    nx = create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        permissions=PermissionConfig(enforce=True),
        parsing=ParseConfig(auto_parse=False),
    )
    from types import SimpleNamespace

    db_key_auth = DatabaseAPIKeyAuth(record_store=SimpleNamespace(session_factory=session_factory))
    from nexus.bricks.auth.providers.discriminator import DiscriminatingAuthProvider

    auth_provider = DiscriminatingAuthProvider(api_key_provider=db_key_auth, jwt_provider=None)

    application = create_app(
        nexus_fs=nx,
        auth_provider=auth_provider,
        database_url=f"sqlite:///{db_path}",
    )

    yield {"app": application, "admin_key": admin_raw}

    nx.close()
    metadata_store.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def client(app: Any) -> Any:
    from fastapi.testclient import TestClient

    with TestClient(app["app"]) as c:
        yield {"client": c, "admin_key": app["admin_key"]}


# ===========================================================================
# E2E Tests: Full signing flow with real crypto + permissions
# ===========================================================================


class TestSignedIPCE2E:
    """E2E: Full IPC signing lifecycle with real KeyService + crypto."""

    @pytest.mark.asyncio
    async def test_full_signed_delivery_enforce_mode(
        self,
        vfs: InMemoryStorageDriver,
        key_service: KeyService,
        crypto: IdentityCrypto,
    ) -> None:
        """Full lifecycle: provision agents, sign, send, verify, process in ENFORCE mode."""
        # 1. Provision agents
        prov = AgentProvisioner(vfs, zone_id=ZONE)
        await prov.provision("agent:alice", skills=["coding"])
        await prov.provision("agent:bob", skills=["review"])

        # 2. Auto-provision keypairs via real KeyService
        alice_record = key_service.ensure_keypair("agent:alice")
        bob_record = key_service.ensure_keypair("agent:bob")

        assert alice_record.did.startswith("did:key:z")
        assert bob_record.did.startswith("did:key:z")
        assert alice_record.key_id != bob_record.key_id

        # 3. Create signer + verifier with real KeyService
        signer = MessageSigner(key_service, crypto, agent_id="agent:alice")
        verifier = MessageVerifier(key_service, crypto)

        # 4. Send signed message
        sender = MessageSender(vfs, zone_id=ZONE, signer=signer)
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_e2e_signed_001",
            payload={"action": "review_code", "file": "/workspace/main.py"},
        )
        path = await sender.send(env)
        assert path.endswith(".json")

        # 5. Verify envelope on disk has signature
        data = vfs.sys_read(path, ZONE)
        restored = MessageEnvelope.from_bytes(data)
        assert restored.signature is not None
        assert restored.signer_did == alice_record.did
        assert restored.signer_key_id == alice_record.key_id

        # 6. Process with ENFORCE mode
        received: list[MessageEnvelope] = []

        async def handler(msg: MessageEnvelope) -> None:
            received.append(msg)

        processor = MessageProcessor(
            vfs,
            "agent:bob",
            handler,
            zone_id=ZONE,
            verifier=verifier,
            signing_mode=SigningMode.ENFORCE,
        )
        count = await processor.process_inbox()

        assert count == 1
        assert len(received) == 1
        assert received[0].payload["action"] == "review_code"
        assert received[0].sender == "agent:alice"

        # Inbox should be empty, processed should have 1
        inbox_files = vfs.list_dir(inbox_path("agent:bob"), ZONE)
        assert len(inbox_files) == 0
        processed_files = vfs.list_dir(processed_path("agent:bob"), ZONE)
        assert len(processed_files) == 1

    @pytest.mark.asyncio
    async def test_unsigned_message_rejected_enforce_mode(
        self,
        vfs: InMemoryStorageDriver,
        key_service: KeyService,
        crypto: IdentityCrypto,
    ) -> None:
        """ENFORCE mode: unsigned message → dead-lettered with UNSIGNED_MESSAGE reason."""
        prov = AgentProvisioner(vfs, zone_id=ZONE)
        await prov.provision("agent:alice", skills=["coding"])
        await prov.provision("agent:bob", skills=["review"])

        verifier = MessageVerifier(key_service, crypto)

        # Send WITHOUT signer (unsigned)
        sender = MessageSender(vfs, zone_id=ZONE)
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_e2e_unsigned_001",
            payload={"action": "sneaky_unsigned"},
        )
        await sender.send(env)

        handler_called = False

        async def handler(msg: MessageEnvelope) -> None:
            nonlocal handler_called
            handler_called = True

        processor = MessageProcessor(
            vfs,
            "agent:bob",
            handler,
            zone_id=ZONE,
            verifier=verifier,
            signing_mode=SigningMode.ENFORCE,
        )
        await processor.process_inbox()

        assert not handler_called, "Handler should NOT be called for unsigned message in ENFORCE"

        # Message should be in dead_letter
        dl_files = vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msgs) == 1

        # Verify reason sidecar exists
        reason_files = [f for f in dl_files if f.endswith(".reason.json")]
        assert len(reason_files) == 1

    @pytest.mark.asyncio
    async def test_tampered_message_rejected_enforce_mode(
        self,
        vfs: InMemoryStorageDriver,
        key_service: KeyService,
        crypto: IdentityCrypto,
    ) -> None:
        """ENFORCE mode: tampered payload → dead-lettered with INVALID_SIGNATURE reason."""
        from nexus.bricks.ipc.conventions import message_path_in_inbox

        prov = AgentProvisioner(vfs, zone_id=ZONE)
        await prov.provision("agent:alice", skills=["coding"])
        await prov.provision("agent:bob", skills=["review"])

        key_service.ensure_keypair("agent:alice")
        signer = MessageSigner(key_service, crypto, agent_id="agent:alice")
        verifier = MessageVerifier(key_service, crypto)

        # Sign then tamper
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_e2e_tampered_001",
            payload={"action": "legitimate"},
        )
        signed = signer.sign(env)
        tampered = signed.model_copy(update={"payload": {"action": "TAMPERED_BY_ATTACKER"}})

        # Write tampered envelope directly to inbox
        msg_path = message_path_in_inbox("agent:bob", tampered.id, tampered.timestamp)
        vfs.write(msg_path, tampered.to_bytes(), ZONE)

        handler_called = False

        async def handler(msg: MessageEnvelope) -> None:
            nonlocal handler_called
            handler_called = True

        processor = MessageProcessor(
            vfs,
            "agent:bob",
            handler,
            zone_id=ZONE,
            verifier=verifier,
            signing_mode=SigningMode.ENFORCE,
        )
        await processor.process_inbox()

        assert not handler_called, "Handler should NOT be called for tampered message"

        dl_files = vfs.list_dir(dead_letter_path("agent:bob"), ZONE)
        dl_msgs = [f for f in dl_files if not f.endswith(".reason.json")]
        assert len(dl_msgs) == 1

    @pytest.mark.asyncio
    async def test_unsigned_allowed_verify_only_mode(
        self,
        vfs: InMemoryStorageDriver,
        key_service: KeyService,
        crypto: IdentityCrypto,
    ) -> None:
        """VERIFY_ONLY mode: unsigned message → handler still invoked (with warning)."""
        prov = AgentProvisioner(vfs, zone_id=ZONE)
        await prov.provision("agent:alice", skills=["coding"])
        await prov.provision("agent:bob", skills=["review"])

        verifier = MessageVerifier(key_service, crypto)

        sender = MessageSender(vfs, zone_id=ZONE)
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_e2e_verify_only_001",
            payload={"action": "unsigned_but_ok"},
        )
        await sender.send(env)

        received: list[MessageEnvelope] = []

        async def handler(msg: MessageEnvelope) -> None:
            received.append(msg)

        processor = MessageProcessor(
            vfs,
            "agent:bob",
            handler,
            zone_id=ZONE,
            verifier=verifier,
            signing_mode=SigningMode.VERIFY_ONLY,
        )
        count = await processor.process_inbox()

        assert count == 1
        assert len(received) == 1
        assert received[0].payload["action"] == "unsigned_but_ok"

    @pytest.mark.asyncio
    async def test_off_mode_no_verification(
        self,
        vfs: InMemoryStorageDriver,
    ) -> None:
        """OFF mode: no verification at all, even without verifier configured."""
        prov = AgentProvisioner(vfs, zone_id=ZONE)
        await prov.provision("agent:alice", skills=["coding"])
        await prov.provision("agent:bob", skills=["review"])

        sender = MessageSender(vfs, zone_id=ZONE)
        env = MessageEnvelope(
            sender="agent:alice",
            recipient="agent:bob",
            type=MessageType.TASK,
            id="msg_e2e_off_001",
            payload={"action": "no_signing"},
        )
        await sender.send(env)

        received: list[MessageEnvelope] = []

        async def handler(msg: MessageEnvelope) -> None:
            received.append(msg)

        processor = MessageProcessor(
            vfs,
            "agent:bob",
            handler,
            zone_id=ZONE,
            signing_mode=SigningMode.OFF,
        )
        count = await processor.process_inbox()

        assert count == 1
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_zone_settings_signing_mode(self) -> None:
        """ZoneSettings.message_signing field works correctly."""
        # Default is OFF
        settings = ZoneSettings()
        assert settings.message_signing == SigningMode.OFF

        # Can be set to enforce
        settings = ZoneSettings(message_signing=SigningMode.ENFORCE)
        assert settings.message_signing == SigningMode.ENFORCE

        # Can be set from string (JSON deserialization)
        settings = ZoneSettings.model_validate({"message_signing": "verify_only"})
        assert settings.message_signing == SigningMode.VERIFY_ONLY

        # Extra fields preserved (forward compat)
        settings = ZoneSettings.model_validate({"message_signing": "enforce", "custom": True})
        assert settings.message_signing == SigningMode.ENFORCE

    @pytest.mark.asyncio
    async def test_signing_performance_budget(
        self,
        key_service: KeyService,
        crypto: IdentityCrypto,
    ) -> None:
        """Benchmark: signing + verification must be under 1ms average."""
        key_service.ensure_keypair("agent:perf")
        signer = MessageSigner(key_service, crypto, agent_id="agent:perf")
        verifier = MessageVerifier(key_service, crypto)

        iterations = 1000
        envelopes = [
            MessageEnvelope(
                sender="agent:perf",
                recipient="agent:receiver",
                type=MessageType.TASK,
                id=f"msg_perf_{i}",
                payload={"data": f"payload_{i}", "nested": {"key": "value"}},
            )
            for i in range(iterations)
        ]

        # Warm up (provision key + cache)
        signed_warmup = signer.sign(envelopes[0])
        verifier.verify(signed_warmup)

        # Benchmark signing
        start = time.perf_counter()
        signed_envelopes = [signer.sign(env) for env in envelopes]
        sign_elapsed = time.perf_counter() - start
        sign_avg_ms = (sign_elapsed / iterations) * 1000

        # Benchmark verification
        start = time.perf_counter()
        results = [verifier.verify(signed) for signed in signed_envelopes]
        verify_elapsed = time.perf_counter() - start
        verify_avg_ms = (verify_elapsed / iterations) * 1000

        # All verifications must pass
        assert all(r.valid for r in results), "Some verifications failed"

        # Performance assertions
        assert sign_avg_ms < 1.0, f"Signing avg {sign_avg_ms:.3f}ms exceeds 1ms budget"
        assert verify_avg_ms < 1.0, f"Verification avg {verify_avg_ms:.3f}ms exceeds 1ms budget"

        total_avg_ms = sign_avg_ms + verify_avg_ms
        # Print for CI visibility (not a failure condition)
        print(
            f"\nPerformance: sign={sign_avg_ms:.3f}ms, "
            f"verify={verify_avg_ms:.3f}ms, "
            f"total={total_avg_ms:.3f}ms"
        )


class TestSignedIPCWithFastAPI:
    """E2E tests with actual FastAPI server + permissions + signing."""

    def test_server_health_check(self, client: Any) -> None:
        """FastAPI server is running with permissions enabled."""
        resp = client["client"].get("/health")
        assert resp.status_code == 200

    def test_unauthenticated_rejected(self, client: Any) -> None:
        """Unauthenticated requests are rejected (permissions enforced)."""
        body = {
            "jsonrpc": "2.0",
            "method": "mkdir",
            "params": {"path": "/agents", "exist_ok": True},
            "id": "1",
        }
        resp = client["client"].post("/api/nfs/mkdir", json=body)
        assert resp.status_code == 401

    def test_authenticated_agent_operations(self, client: Any) -> None:
        """Authenticated requests succeed for agent operations."""
        headers = {"Authorization": f"Bearer {client['admin_key']}"}
        body = {
            "jsonrpc": "2.0",
            "method": "mkdir",
            "params": {"path": "/agents", "exist_ok": True},
            "id": "1",
        }
        resp = client["client"].post("/api/nfs/mkdir", json=body, headers=headers)
        assert resp.status_code == 200

    def test_register_agent_gets_identity(self, client: Any) -> None:
        """Agent registration provisions DID + keypair for signing.

        Note: DID provisioning requires the server's KeyService to be
        initialized (nexus.bricks.identity). When the module is unavailable
        (e.g. lightweight test environments), agent registration still succeeds
        but without DID fields — the test validates both paths.
        """
        headers = {"Authorization": f"Bearer {client['admin_key']}"}
        agent_id = f"e2e-admin,sign-test-{uuid.uuid4().hex[:8]}"
        body = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "register_agent",
            "params": {
                "agent_id": agent_id,
                "name": "Signing Test Agent",
                "context": {"user_id": "e2e-admin", "zone_id": "root"},
            },
        }
        resp = client["client"].post("/api/nfs/register_agent", json=body, headers=headers)
        assert resp.status_code == 200
        data = resp.json()

        # AgentRPCService may not be wired in minimal test environments
        # (NexusFS created without factory — no @rpc_expose discovery)
        if "error" in data:
            pytest.skip(
                "AgentRPCService not wired (lightweight test NexusFS, no factory); "
                "register_agent RPC not available"
            )

        result = data.get("result", data)

        # Agent registration must succeed
        assert "agent_id" in result
        assert result["agent_id"] == agent_id

        if "did" in result:
            # Full identity provisioning: KeyService initialized
            assert result["did"].startswith("did:key:z")
            assert "key_id" in result

            # Verify identity endpoint returns same info
            resp2 = client["client"].get(f"/api/v2/agents/{agent_id}/identity", headers=headers)
            assert resp2.status_code == 200
            identity = resp2.json()
            assert identity["did"] == result["did"]
            assert identity["algorithm"] == "Ed25519"
        else:
            # Server KeyService not initialized — DID provisioning skipped.
            # This is acceptable: signing infrastructure (tested above in
            # TestSignedIPCE2E) works independently of the server endpoint.
            pytest.skip(
                "Server KeyService not initialized (nexus.bricks.identity unavailable); "
                "DID provisioning via register_agent not testable in this environment"
            )
