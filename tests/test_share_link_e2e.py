#!/usr/bin/env python3
"""End-to-end tests for Share Link feature (Issue #227).

TRUE E2E tests that:
1. Start an actual FastAPI server on a real port
2. Use PostgreSQL database
3. Make real HTTP requests over the network
4. Test the full stack including network layer

Usage:
    # Start PostgreSQL first (via docker-compose or local)
    docker-compose -f docker-compose.demo.yml up -d postgres

    # Run the E2E tests
    PYTHONPATH=src python tests/test_share_link_e2e.py

    # Or with custom PostgreSQL URL
    PYTHONPATH=src DATABASE_URL=postgresql://user:pass@localhost:5432/nexus python tests/test_share_link_e2e.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import httpx

# ==============================================================================
# Configuration
# ==============================================================================

TEST_PORT = 19227
TEST_HOST = "127.0.0.1"
BASE_URL = f"http://{TEST_HOST}:{TEST_PORT}"

# PostgreSQL connection - use environment variable or default
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:nexus@localhost:5432/nexus")

# Server script that will be run as a subprocess
SERVER_SCRIPT = """
import os
import sys

import uvicorn
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS
from nexus.server.auth.auth_routes import set_auth_provider
from nexus.server.auth.database_local import DatabaseLocalAuth
from nexus.server.fastapi_server import create_app

database_url = os.environ["DATABASE_URL"]
files_dir = os.environ["NEXUS_E2E_FILES_DIR"]
host = os.environ.get("NEXUS_E2E_HOST", "127.0.0.1")
port = int(os.environ.get("NEXUS_E2E_PORT", "19227"))

print(f"Starting E2E server on {host}:{port}")
print(f"Database: {database_url.split('@')[1] if '@' in database_url else database_url}")

backend = LocalBackend(root_path=files_dir)
metadata_store = RaftMetadataStore.local(str(database_url).replace(".db", ""))
nx = NexusFS(backend=backend, metadata_store=metadata_store, enforce_permissions=False)

session_factory = sessionmaker(bind=nx.metadata.engine)
auth_provider = DatabaseLocalAuth(
    session_factory=session_factory,
    jwt_secret="test-secret-key-for-e2e-testing",
)
set_auth_provider(auth_provider)

app = create_app(
    nexus_fs=nx,
    auth_provider=auth_provider,
    database_url=database_url,
)

uvicorn.run(app, host=host, port=port, log_level="warning")
"""

# JWT secret must match server
JWT_SECRET = "test-secret-key-for-e2e-testing"


# ==============================================================================
# Helper Functions
# ==============================================================================


def wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    """Wait for a port to be available."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except (TimeoutError, ConnectionRefusedError, OSError):
            time.sleep(0.2)
    return False


def print_result(test_name: str, passed: bool, message: str = ""):
    """Print test result."""
    status = "PASS" if passed else "FAIL"
    color = "\033[92m" if passed else "\033[91m"
    reset = "\033[0m"
    print(f"  [{color}{status}{reset}] {test_name}")
    if message and not passed:
        print(f"        {message}")


class E2ETestRunner:
    """Run E2E tests for share links."""

    def __init__(self):
        self.temp_dir = None
        self.process = None
        self.log_handle = None
        self.client = httpx.Client(timeout=30.0, trust_env=False)
        self.auth_headers = None
        self.user_id = None
        self.passed = 0
        self.failed = 0

    def setup(self):
        """Start the server and create test user."""
        print("\n" + "=" * 60)
        print("Setting up E2E test environment")
        print("=" * 60)

        # Create temp directory for files
        self.temp_dir = tempfile.mkdtemp(prefix="nexus_e2e_")
        files_dir = os.path.join(self.temp_dir, "files")
        os.makedirs(files_dir, exist_ok=True)

        # Write server script
        script_path = os.path.join(self.temp_dir, "server.py")
        with open(script_path, "w") as f:
            f.write(SERVER_SCRIPT)

        # Setup environment
        env = os.environ.copy()
        env["DATABASE_URL"] = DATABASE_URL
        env["NEXUS_E2E_FILES_DIR"] = files_dir
        env["NEXUS_E2E_HOST"] = TEST_HOST
        env["NEXUS_E2E_PORT"] = str(TEST_PORT)
        env["PYTHONPATH"] = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
        )

        # Start server
        log_file = os.path.join(self.temp_dir, "server.log")
        self.log_handle = open(log_file, "w")  # noqa: SIM115
        self.process = subprocess.Popen(
            [sys.executable, script_path],
            env=env,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )

        print(f"  Starting server on {BASE_URL}...")

        # Wait for server
        if not wait_for_port(TEST_HOST, TEST_PORT, timeout=30.0):
            self.log_handle.close()
            with open(log_file) as f:
                print(f"  Server failed to start:\n{f.read()}")
            raise RuntimeError("Server failed to start")

        # Verify health
        time.sleep(0.5)
        response = self.client.get(f"{BASE_URL}/health")
        if response.status_code != 200:
            raise RuntimeError(f"Health check failed: {response.status_code}")

        print("  Server started successfully")

        # Create test user
        self._create_test_user()
        print("  Test user created")
        print()

    def _create_test_user(self):
        """Create and authenticate a test user via direct database access."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from nexus.server.auth.database_local import DatabaseLocalAuth
        from nexus.storage.models import Base

        unique_id = str(uuid.uuid4())[:8]
        email = f"e2e_test_{unique_id}@example.com"
        password = "testpassword123"
        username = f"e2e_user_{unique_id}"

        # Connect to same database as server
        engine = create_engine(DATABASE_URL)

        # Create tables if they don't exist
        Base.metadata.create_all(engine)

        session_factory = sessionmaker(bind=engine)
        auth_provider = DatabaseLocalAuth(
            session_factory=session_factory,
            jwt_secret=JWT_SECRET,
        )

        # Register user directly
        user = auth_provider.register_user(
            email=email,
            password=password,
            username=username,
        )

        # Create JWT token
        user_info = {
            "subject_id": user.user_id,
            "subject_type": "user",
            "zone_id": "default",
            "email": user.email,
            "name": user.display_name or user.username or user.email,
        }
        token = auth_provider.create_token(user.email, user_info)

        self.auth_headers = {"Authorization": f"Bearer {token}"}
        self.user_id = user.user_id

    def teardown(self):
        """Stop server and cleanup."""
        print("\n" + "=" * 60)
        print("Cleaning up")
        print("=" * 60)

        if self.client:
            self.client.close()

        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            print("  Server stopped")

        if self.log_handle:
            self.log_handle.close()

        if self.temp_dir:
            import shutil

            shutil.rmtree(self.temp_dir, ignore_errors=True)
            print("  Temp directory cleaned")

    def run_test(self, test_name: str, test_func):
        """Run a single test."""
        try:
            test_func()
            print_result(test_name, True)
            self.passed += 1
        except AssertionError as e:
            print_result(test_name, False, str(e))
            self.failed += 1
        except Exception as e:
            print_result(test_name, False, f"Exception: {e}")
            self.failed += 1

    # ==========================================================================
    # Test Cases
    # ==========================================================================

    def test_health_check(self):
        """Verify server health."""
        response = self.client.get(f"{BASE_URL}/health")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        assert response.json()["status"] == "healthy"

    def test_create_share_link(self):
        """Test creating a share link."""
        # First create a file
        file_path = f"/e2e-test-{time.time()}.txt"
        response = self.client.post(
            f"{BASE_URL}/api/nfs/write",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "params": {"path": file_path, "content": "Test content"},
            },
            headers=self.auth_headers,
        )
        assert response.status_code == 200, f"Failed to create file: {response.text}"

        # Create share link
        response = self.client.post(
            f"{BASE_URL}/api/nfs/create_share_link",
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "params": {"path": file_path, "permission_level": "viewer"},
            },
            headers=self.auth_headers,
        )
        assert response.status_code == 200, f"Failed to create share link: {response.text}"
        result = response.json()
        assert result["result"]["error_message"] is None
        assert "link_id" in result["result"]["data"]

    def test_share_link_access(self):
        """Test accessing a share link anonymously."""
        # Create file and share link
        file_path = f"/e2e-access-{time.time()}.txt"
        content = "Content for anonymous access test"

        self.client.post(
            f"{BASE_URL}/api/nfs/write",
            json={"jsonrpc": "2.0", "id": "1", "params": {"path": file_path, "content": content}},
            headers=self.auth_headers,
        )

        response = self.client.post(
            f"{BASE_URL}/api/nfs/create_share_link",
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "params": {"path": file_path, "permission_level": "viewer"},
            },
            headers=self.auth_headers,
        )
        link_id = response.json()["result"]["data"]["link_id"]

        # Access anonymously (no auth headers)
        response = self.client.post(f"{BASE_URL}/api/share/{link_id}/access", json={})
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()
        assert data["access_granted"] is True
        assert data["path"] == file_path

    def test_share_link_revocation(self):
        """Test revoking a share link."""
        # Create file and share link
        file_path = f"/e2e-revoke-{time.time()}.txt"

        self.client.post(
            f"{BASE_URL}/api/nfs/write",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "params": {"path": file_path, "content": "Revoke test"},
            },
            headers=self.auth_headers,
        )

        response = self.client.post(
            f"{BASE_URL}/api/nfs/create_share_link",
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "params": {"path": file_path, "permission_level": "viewer"},
            },
            headers=self.auth_headers,
        )
        link_id = response.json()["result"]["data"]["link_id"]

        # Revoke
        response = self.client.post(
            f"{BASE_URL}/api/nfs/revoke_share_link",
            json={"jsonrpc": "2.0", "id": "3", "params": {"link_id": link_id}},
            headers=self.auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["result"]["data"]["revoked"] is True

        # Verify access denied (410 Gone)
        response = self.client.post(f"{BASE_URL}/api/share/{link_id}/access", json={})
        assert response.status_code == 410, f"Expected 410, got {response.status_code}"

    def test_share_link_password(self):
        """Test password-protected share links."""
        file_path = f"/e2e-password-{time.time()}.txt"
        password = "secretpass123"

        self.client.post(
            f"{BASE_URL}/api/nfs/write",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "params": {"path": file_path, "content": "Password protected"},
            },
            headers=self.auth_headers,
        )

        response = self.client.post(
            f"{BASE_URL}/api/nfs/create_share_link",
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "params": {"path": file_path, "permission_level": "viewer", "password": password},
            },
            headers=self.auth_headers,
        )
        link_id = response.json()["result"]["data"]["link_id"]

        # No password - should fail
        response = self.client.post(f"{BASE_URL}/api/share/{link_id}/access", json={})
        assert response.status_code == 401, (
            f"Expected 401 without password, got {response.status_code}"
        )

        # Wrong password - should fail
        response = self.client.post(
            f"{BASE_URL}/api/share/{link_id}/access", json={"password": "wrong"}
        )
        assert response.status_code == 401, (
            f"Expected 401 with wrong password, got {response.status_code}"
        )

        # Correct password - should succeed
        response = self.client.post(
            f"{BASE_URL}/api/share/{link_id}/access", json={"password": password}
        )
        assert response.status_code == 200, (
            f"Expected 200 with correct password, got {response.status_code}"
        )
        assert response.json()["access_granted"] is True

    def test_share_link_access_limit(self):
        """Test share link access limits."""
        file_path = f"/e2e-limit-{time.time()}.txt"
        max_accesses = 3

        self.client.post(
            f"{BASE_URL}/api/nfs/write",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "params": {"path": file_path, "content": "Limited access"},
            },
            headers=self.auth_headers,
        )

        response = self.client.post(
            f"{BASE_URL}/api/nfs/create_share_link",
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "params": {
                    "path": file_path,
                    "permission_level": "viewer",
                    "max_access_count": max_accesses,
                },
            },
            headers=self.auth_headers,
        )
        link_id = response.json()["result"]["data"]["link_id"]

        # Access max_accesses times - should succeed
        for i in range(max_accesses):
            response = self.client.post(f"{BASE_URL}/api/share/{link_id}/access", json={})
            assert response.status_code == 200, f"Access {i + 1} failed: {response.status_code}"
            assert response.json()["remaining_accesses"] == max_accesses - i - 1

        # Next access should fail (429 Too Many Requests)
        response = self.client.post(f"{BASE_URL}/api/share/{link_id}/access", json={})
        assert response.status_code == 429, f"Expected 429 after limit, got {response.status_code}"

    def test_list_share_links(self):
        """Test listing share links."""
        file_path = f"/e2e-list-{time.time()}.txt"

        self.client.post(
            f"{BASE_URL}/api/nfs/write",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "params": {"path": file_path, "content": "List test"},
            },
            headers=self.auth_headers,
        )

        self.client.post(
            f"{BASE_URL}/api/nfs/create_share_link",
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "params": {"path": file_path, "permission_level": "editor"},
            },
            headers=self.auth_headers,
        )

        response = self.client.post(
            f"{BASE_URL}/api/nfs/list_share_links",
            json={"jsonrpc": "2.0", "id": "3", "params": {}},
            headers=self.auth_headers,
        )
        assert response.status_code == 200
        result = response.json()["result"]["data"]
        assert result["count"] >= 1

    def test_access_logs(self):
        """Test access logging."""
        file_path = f"/e2e-logs-{time.time()}.txt"

        self.client.post(
            f"{BASE_URL}/api/nfs/write",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "params": {"path": file_path, "content": "Log test"},
            },
            headers=self.auth_headers,
        )

        response = self.client.post(
            f"{BASE_URL}/api/nfs/create_share_link",
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "params": {"path": file_path, "permission_level": "viewer"},
            },
            headers=self.auth_headers,
        )
        link_id = response.json()["result"]["data"]["link_id"]

        # Access a few times
        for _ in range(3):
            self.client.post(f"{BASE_URL}/api/share/{link_id}/access", json={})

        # Get logs
        response = self.client.post(
            f"{BASE_URL}/api/nfs/get_share_link_access_logs",
            json={"jsonrpc": "2.0", "id": "3", "params": {"link_id": link_id}},
            headers=self.auth_headers,
        )
        assert response.status_code == 200
        result = response.json()["result"]["data"]
        assert result["count"] >= 3

    def run_all(self):
        """Run all tests."""
        print("\n" + "=" * 60)
        print("Running Share Link E2E Tests")
        print("=" * 60)
        print(f"  Database: {DATABASE_URL.split('@')[1] if '@' in DATABASE_URL else DATABASE_URL}")
        print(f"  Server: {BASE_URL}")
        print()

        tests = [
            ("Health Check", self.test_health_check),
            ("Create Share Link", self.test_create_share_link),
            ("Share Link Access", self.test_share_link_access),
            ("Share Link Revocation", self.test_share_link_revocation),
            ("Password Protected Link", self.test_share_link_password),
            ("Access Limit", self.test_share_link_access_limit),
            ("List Share Links", self.test_list_share_links),
            ("Access Logs", self.test_access_logs),
        ]

        for test_name, test_func in tests:
            self.run_test(test_name, test_func)

        # Summary
        print("\n" + "=" * 60)
        total = self.passed + self.failed
        print(f"Results: {self.passed}/{total} passed")
        if self.failed > 0:
            print(f"\033[91m{self.failed} tests FAILED\033[0m")
        else:
            print("\033[92mAll tests PASSED\033[0m")
        print("=" * 60)

        return self.failed == 0


def main():
    """Main entry point."""
    runner = E2ETestRunner()
    success = False

    try:
        runner.setup()
        success = runner.run_all()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"\n\033[91mSetup failed: {e}\033[0m")
        import traceback

        traceback.print_exc()
    finally:
        runner.teardown()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
