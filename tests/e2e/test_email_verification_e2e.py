"""E2E test for email verification flow (Issue #1434).

Uses the real FastAPI server (create_app) with:
- NexusFS + RaftMetadataStore
- DiscriminatingAuthProvider (API key + JWT)
- NEXUS_ENFORCE_PERMISSIONS=true
- Full auth routes (/auth/register, /auth/login, etc.)

Validates the complete email verification lifecycle:
1. Register user → 201
2. Attempt login → 401 (email not verified)
3. Request verification → 202
4. Verify email with token → 200
5. Login → 200 (succeeds with JWT)
6. Use JWT to access /auth/me → 200 (profile)
7. Non-user (unauthenticated) operations → 401

Run with:
    pytest tests/e2e/test_email_verification_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS
from nexus.factory import create_nexus_fs
from nexus.raft import _HAS_METASTORE
from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.server.auth.database_local import DatabaseLocalAuth
from nexus.server.auth.factory import DiscriminatingAuthProvider
from nexus.storage.models import Base
from nexus.storage.record_store import SQLAlchemyRecordStore

# Skip entire module if native Metastore is not built
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _HAS_METASTORE,
        reason="Requires native _nexus_raft module (maturin develop)",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_nexus_fs(tmp_path: Path, *, enforce_permissions: bool = False) -> NexusFS:
    """Create a real NexusFS with RaftMetadataStore."""
    from nexus.storage.raft_metadata_store import RaftMetadataStore

    storage_path = tmp_path / "storage_email_verify"
    storage_path.mkdir(exist_ok=True)
    backend = LocalBackend(root_path=storage_path)

    raft_dir = str(tmp_path / "raft-metadata-email-verify")
    metadata_store = RaftMetadataStore.embedded(raft_dir)

    record_store = SQLAlchemyRecordStore(db_url=f"sqlite:///{tmp_path / 'records_email_verify.db'}")

    return create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        enforce_permissions=enforce_permissions,
        is_admin=False,
    )


class _TokenCapture(logging.Handler):
    """Logging handler that captures verification URLs."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        if "[EMAIL VERIFICATION]" in msg and "URL:" in msg:
            url = msg.split("URL:")[1].strip()
            self.urls.append(url)


def _rpc_body(method: str, params: dict | None = None) -> str:
    """Build JSON-RPC request body."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params or {},
        }
    )


def _rpc_post(
    client: TestClient,
    method: str,
    params: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, dict]:
    """Make RPC call, return (status_code, parsed_json)."""
    default_headers = {"Content-Type": "application/json"}
    if headers:
        default_headers.update(headers)
    resp = client.post(
        f"/api/nfs/{method}",
        content=_rpc_body(method, params),
        headers=default_headers,
    )
    return resp.status_code, resp.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _env(tmp_path: Path, monkeypatch):
    """Set up environment and database for the full-stack E2E test."""
    # Create database for auth
    db_path = tmp_path / "email_verify_e2e.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    # Create NexusFS with permissions enforced
    nx = _create_nexus_fs(tmp_path, enforce_permissions=True)

    # Build DiscriminatingAuthProvider (same as production `nexus serve`)
    jwt_secret = "e2e-test-jwt-secret-for-verification"
    local_auth = DatabaseLocalAuth(
        session_factory=session_factory,
        jwt_secret=jwt_secret,
        token_expiry=3600,
    )
    api_key_auth = DatabaseAPIKeyAuth(session_factory=session_factory)
    auth_provider = DiscriminatingAuthProvider(
        api_key_provider=api_key_auth,
        jwt_provider=local_auth,
    )

    # Disable search daemon to avoid background threads
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")

    # Create FastAPI app with create_app (same as production)
    from nexus.server.fastapi_server import create_app

    app = create_app(
        nexus_fs=nx,
        auth_provider=auth_provider,
        database_url=db_url,
    )

    yield {
        "app": app,
        "nx": nx,
        "session_factory": session_factory,
        "local_auth": local_auth,
        "engine": engine,
    }

    nx.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def client(_env):
    """TestClient wired to the full FastAPI app."""
    with TestClient(_env["app"], raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def local_auth(_env) -> DatabaseLocalAuth:
    """The DatabaseLocalAuth instance for direct method access in tests."""
    return _env["local_auth"]


# ---------------------------------------------------------------------------
# Tests: Full Email Verification Flow with Real Server
# ---------------------------------------------------------------------------


class TestEmailVerificationFullStack:
    """Full email verification flow against create_app() with permissions enabled."""

    def test_full_verification_flow(
        self, client: TestClient, local_auth: DatabaseLocalAuth
    ) -> None:
        """Register → fail login → request verify → verify → login → use JWT."""
        email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
        password = "securePassword123"

        # 1. Register user → 201
        resp = client.post(
            "/auth/register",
            json={"email": email, "password": password},
        )
        assert resp.status_code == 201, f"Register failed: {resp.json()}"
        data = resp.json()
        assert data["email"] == email
        assert data["user_id"]
        assert data["token"]  # Registration returns a JWT

        # 2. Attempt login → 401 (email not verified)
        resp = client.post(
            "/auth/login",
            json={"identifier": email, "password": password},
        )
        assert resp.status_code == 401, (
            f"Expected 401 (unverified), got {resp.status_code}: {resp.json()}"
        )
        assert "not verified" in resp.json()["detail"].lower()

        # 3. Request verification → 202 (always, prevents email enumeration)
        handler = _TokenCapture()
        handler.setLevel(logging.DEBUG)
        log = logging.getLogger("nexus.server.auth.email_sender")
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)

        try:
            resp = client.post(
                "/auth/request-verification",
                json={"email": email},
            )
            assert resp.status_code == 202
        finally:
            log.removeHandler(handler)

        # 4. Extract token from captured LogEmailSender output
        assert len(handler.urls) == 1, f"Expected 1 URL, got {handler.urls}"
        verification_url = handler.urls[0]
        token = verification_url.split("token=")[1]

        # 5. Verify email → 200
        resp = client.post(
            "/auth/verify-email",
            json={"token": token},
        )
        assert resp.status_code == 200, f"Verify failed: {resp.json()}"
        assert "verified" in resp.json()["message"].lower()

        # 6. Login → 200 (now succeeds)
        resp = client.post(
            "/auth/login",
            json={"identifier": email, "password": password},
        )
        assert resp.status_code == 200, f"Login failed after verification: {resp.json()}"
        login_data = resp.json()
        assert "token" in login_data
        jwt_token = login_data["token"]
        assert login_data["user"]["email"] == email

        # 7. Use JWT to access /auth/me → 200
        resp = client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        assert resp.status_code == 200, f"Profile fetch failed: {resp.json()}"
        profile = resp.json()
        assert profile["email"] == email

    def test_unverified_user_cannot_login(self, client: TestClient) -> None:
        """Register a user and confirm login is blocked without verification."""
        email = f"unverified-{uuid.uuid4().hex[:8]}@example.com"
        password = "password12345678"

        # Register
        resp = client.post(
            "/auth/register",
            json={"email": email, "password": password},
        )
        assert resp.status_code == 201

        # Login must fail
        resp = client.post(
            "/auth/login",
            json={"identifier": email, "password": password},
        )
        assert resp.status_code == 401
        assert "not verified" in resp.json()["detail"].lower()

    def test_request_verification_prevents_email_enumeration(self, client: TestClient) -> None:
        """Endpoint returns 202 regardless of whether email exists (prevents enumeration)."""
        resp = client.post(
            "/auth/request-verification",
            json={"email": "totally-fake-nonexistent@example.com"},
        )
        assert resp.status_code == 202

    def test_invalid_verification_token_rejected(self, client: TestClient) -> None:
        """Invalid verification token returns 400."""
        resp = client.post(
            "/auth/verify-email",
            json={"token": "not-a-valid-jwt-token"},
        )
        assert resp.status_code == 400

    def test_expired_verification_token_rejected(
        self, client: TestClient, local_auth: DatabaseLocalAuth
    ) -> None:
        """An expired verification token is rejected at /auth/verify-email."""
        import time as time_mod

        from authlib.jose import jwt as jose_jwt

        # Craft an already-expired token
        header = {"alg": "HS256"}
        payload = {
            "sub": "fake-user-id",
            "email": "expired@example.com",
            "purpose": "email_verify",
            "iat": int(time_mod.time()) - 100000,
            "exp": int(time_mod.time()) - 1,
        }
        expired_token = jose_jwt.encode(header, payload, local_auth.jwt_secret)
        token_str = expired_token.decode() if isinstance(expired_token, bytes) else expired_token

        resp = client.post(
            "/auth/verify-email",
            json={"token": token_str},
        )
        assert resp.status_code == 400

    def test_wrong_purpose_token_rejected(
        self, client: TestClient, local_auth: DatabaseLocalAuth
    ) -> None:
        """A JWT with wrong purpose claim is rejected at /auth/verify-email."""
        import time as time_mod

        from authlib.jose import jwt as jose_jwt

        header = {"alg": "HS256"}
        payload = {
            "sub": "fake-user-id",
            "email": "wrong-purpose@example.com",
            "purpose": "password_reset",  # wrong purpose
            "iat": int(time_mod.time()),
            "exp": int(time_mod.time()) + 3600,
        }
        bad_token = jose_jwt.encode(header, payload, local_auth.jwt_secret)
        token_str = bad_token.decode() if isinstance(bad_token, bytes) else bad_token

        resp = client.post(
            "/auth/verify-email",
            json={"token": token_str},
        )
        assert resp.status_code == 400
        assert "not an email verification token" in resp.json()["detail"].lower()


class TestNonUserPermissionEnforcement:
    """Validate that non-authenticated (non-user) operations are properly denied
    when permissions are enforced."""

    def test_unauthenticated_rpc_rejected(self, client: TestClient) -> None:
        """RPC calls without auth header must get 401."""
        status, body = _rpc_post(
            client,
            "list",
            {"path": "/"},
            # No Authorization header
        )
        assert status == 401, f"Expected 401 for unauthenticated RPC, got {status}: {body}"

    def test_invalid_jwt_rejected(self, client: TestClient) -> None:
        """RPC calls with a fabricated JWT must get 401."""
        status, body = _rpc_post(
            client,
            "list",
            {"path": "/"},
            headers={"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.fake.payload"},
        )
        assert status == 401, f"Expected 401 for invalid JWT, got {status}: {body}"

    def test_invalid_api_key_rejected(self, client: TestClient) -> None:
        """RPC calls with a fabricated API key must get 401."""
        status, body = _rpc_post(
            client,
            "list",
            {"path": "/"},
            headers={"Authorization": "Bearer sk-bogus_fake_aaaabbbb_ccccddddeeeeffffgggg"},
        )
        assert status == 401, f"Expected 401 for invalid API key, got {status}: {body}"

    def test_unverified_user_jwt_still_authenticates_rpc(self, client: TestClient) -> None:
        """A JWT from registration (unverified user) should still authenticate RPC.

        The email verification check is on /auth/login, not on the JWT itself.
        The registration JWT is still valid for API calls — this is by design
        so the user can call /auth/request-verification while logged in.
        """
        email = f"rpc-user-{uuid.uuid4().hex[:8]}@example.com"
        password = "securePassword123"

        # Register — get back a JWT
        resp = client.post(
            "/auth/register",
            json={"email": email, "password": password},
        )
        assert resp.status_code == 201
        reg_jwt = resp.json()["token"]

        # Use the registration JWT for /auth/me (should work even if unverified)
        resp = client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {reg_jwt}"},
        )
        assert resp.status_code == 200, (
            f"Expected 200 for /auth/me with reg JWT, got {resp.status_code}: {resp.json()}"
        )

    def test_verified_user_jwt_works_for_rpc(self, client: TestClient) -> None:
        """A verified user's login JWT can make authenticated RPC calls."""
        email = f"verified-rpc-{uuid.uuid4().hex[:8]}@example.com"
        password = "securePassword123"

        # Register
        resp = client.post(
            "/auth/register",
            json={"email": email, "password": password},
        )
        assert resp.status_code == 201

        # Capture verification token from logs
        handler = _TokenCapture()
        handler.setLevel(logging.DEBUG)
        log = logging.getLogger("nexus.server.auth.email_sender")
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)

        try:
            resp = client.post(
                "/auth/request-verification",
                json={"email": email},
            )
            assert resp.status_code == 202
        finally:
            log.removeHandler(handler)

        assert len(handler.urls) == 1
        token = handler.urls[0].split("token=")[1]

        # Verify email
        resp = client.post("/auth/verify-email", json={"token": token})
        assert resp.status_code == 200

        # Login
        resp = client.post(
            "/auth/login",
            json={"identifier": email, "password": password},
        )
        assert resp.status_code == 200
        jwt_token = resp.json()["token"]

        # Use JWT for RPC call (list files)
        status, body = _rpc_post(
            client,
            "list",
            {"path": "/"},
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        # Should authenticate successfully (200 with result, not 401)
        assert status == 200, f"Expected 200 for authenticated RPC, got {status}: {body}"
