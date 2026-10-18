#!/usr/bin/env python3
"""Exercise the Rust account binary against accounts created by the Python implementation."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import queue
import re
import socketserver
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import grpc
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from nexus.bricks.auth.providers.database_local import DatabaseLocalAuth
from nexus.storage.models import UserModel

ROOT = Path(__file__).resolve().parents[1]
SECRET = "fixture-account-signing-key-32-bytes-minimum"
PASSWORD = "python-created-password-多字节"
NEXT_PASSWORD = "rust-changed-password-✓"
MAIL_ENV = (
    "NEXUS_ACCOUNTS_SMTP_URL",
    "NEXUS_ACCOUNTS_EMAIL_FROM",
    "NEXUS_ACCOUNTS_VERIFICATION_URL",
    "NEXUS_ACCOUNTS_SMTP_USERNAME",
    "NEXUS_ACCOUNTS_SMTP_PASSWORD",
)


@contextmanager
def smtp_capture():
    """A real loopback SMTP conversation; only the delivery provider is replaced."""
    deliveries: queue.Queue = queue.Queue()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(10)
            self.wfile.write(b"220 localhost test relay\r\n")
            while line := self.rfile.readline(65537):
                command = line.split(None, 1)[0].upper()
                if command in {b"EHLO", b"HELO"}:
                    self.wfile.write(b"250-localhost\r\n250 8BITMIME\r\n")
                elif command == b"DATA":
                    self.wfile.write(b"354 Send message\r\n")
                    data = bytearray()
                    while (line := self.rfile.readline(65537)) != b".\r\n":
                        if not line or len(data) > 65536:
                            raise ValueError("Incomplete or oversized test SMTP message")
                        data.extend(line[1:] if line.startswith(b"..") else line)
                    deliveries.put(BytesParser(policy=policy.default).parsebytes(data))
                    self.wfile.write(b"250 Delivered\r\n")
                elif command == b"QUIT":
                    self.wfile.write(b"221 Goodbye\r\n")
                    return
                elif command in {b"MAIL", b"RCPT", b"RSET", b"NOOP"}:
                    self.wfile.write(b"250 OK\r\n")
                else:
                    self.wfile.write(b"502 Unsupported command\r\n")

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as smtp:
        smtp.daemon_threads = True
        thread = threading.Thread(target=lambda: smtp.serve_forever(poll_interval=0.05))
        thread.start()
        try:
            yield smtp.server_address[1], deliveries
        finally:
            smtp.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def request(
    base: str,
    path: str,
    token: str | None = None,
    body: dict | None = None,
    method: str | None = None,
) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        assert response.headers.get("Cache-Control") == "no-store"
        return response.status, json.load(response)


@contextmanager
def server(binary: Path, database_url: str, log_path: Path, smtp_port: int | None = None):
    env = os.environ.copy()
    for name in MAIL_ENV:
        env.pop(name, None)
    env["NEXUS_DATABASE_URL"] = database_url
    env["NEXUS_JWT_SECRET"] = SECRET
    if smtp_port is not None:
        env.update(
            {
                "NEXUS_ACCOUNTS_SMTP_URL": f"smtp://127.0.0.1:{smtp_port}",
                "NEXUS_ACCOUNTS_EMAIL_FROM": "nexus@example.test",
                "NEXUS_ACCOUNTS_VERIFICATION_URL": "https://nexus.example.test/verify-email",
            }
        )
    with log_path.open("w", encoding="utf8") as log:
        process = subprocess.Popen(
            [str(binary), "--grpc-addr", "127.0.0.1:0", "--http-addr", "127.0.0.1:0"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
            encoding="utf8",
        )
        lines: queue.Queue[str] = queue.Queue()
        reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
        reader.start()
        try:
            ready = json.loads(lines.get(timeout=30))
            yield ready
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            process.stdout.close()
            reader.join(timeout=5)
            if os.name != "nt":
                assert process.returncode == 0, (
                    "Account binary did not shut down gracefully on SIGTERM"
                )


def fixture_url(args, directory: Path) -> str:
    if not args.postgres_test_url:
        return f"sqlite:///{directory / 'accounts.db'}"
    parsed = make_url(args.postgres_test_url)
    if (
        parsed.drivername not in {"postgresql+psycopg2", "postgresql+psycopg", "postgresql"}
        or parsed.host not in {"localhost", "127.0.0.1", "::1"}
        or not (parsed.database or "").startswith("nexus_accounts_test")
    ):
        raise ValueError(
            "PostgreSQL acceptance requires a loopback database named nexus_accounts_test*"
        )
    return args.postgres_test_url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--postgres-test-url")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    groups = []
    with (
        tempfile.TemporaryDirectory(prefix="nexus-account-acceptance-") as name,
        smtp_capture() as smtp,
    ):
        fixture = Path(name)
        generated = fixture / "generated"
        generated.mkdir()
        proto_dir = ROOT / "rust/services/proto/nexus/accounts/v1"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "grpc_tools.protoc",
                f"-I{proto_dir}",
                f"--python_out={generated}",
                f"--grpc_python_out={generated}",
                str(proto_dir / "accounts.proto"),
            ],
            check=True,
        )
        sys.path.insert(0, str(generated))
        pb = importlib.import_module("accounts_pb2")
        rpc = importlib.import_module("accounts_pb2_grpc")

        url = fixture_url(args, fixture)
        engine = create_engine(url)
        try:
            if inspect(engine).get_table_names():
                raise ValueError(
                    "Acceptance database must be empty; refusing to modify existing tables"
                )
            UserModel.__table__.create(engine)
            sessions = sessionmaker(engine)
            legacy = DatabaseLocalAuth(sessions, jwt_secret=SECRET)
            user = legacy.register_user(
                "existing@example.test", PASSWORD, username="existing", display_name="既有账号"
            )
            with sessions.begin() as session:
                stored = session.get(UserModel, user.user_id)
                stored.email_verified = 1
                stored.zone_id = "fixture-zone"
                stored.user_metadata = '{"preserved":true}'
                session.add(stored)
            legacy_token = legacy.login("existing", PASSWORD)
            assert legacy_token
            pending = legacy.register_user("pending@example.test", PASSWORD, username="pending")
            old_pending = legacy.register_user("old-pending@example.test", PASSWORD)
            old_verification = legacy.create_email_verification_token(
                old_pending.user_id, old_pending.email
            )
            with server(
                args.binary.resolve(), url, args.output_dir / "accounts-first.log", smtp[0]
            ) as ready:
                base = f"http://{ready['http_addr']}"
                with grpc.insecure_channel(ready["grpc_addr"]) as channel:
                    grpc.channel_ready_future(channel).result(timeout=15)
                    client = rpc.AccountsServiceStub(channel)
                    metadata = [("authorization", f"Bearer {legacy_token}")]
                    profile = client.GetProfile(
                        pb.GetProfileRequest(), metadata=metadata, timeout=15
                    )
                    assert profile.user_id == user.user_id and profile.display_name == "既有账号"
                    status, account = request(base, "/auth/me", legacy_token)
                    assert status == 200 and account["user_id"] == user.user_id
                    groups.append("Python account and JWT -> Rust HTTP/gRPC profile")

                    login = client.Login(
                        pb.LoginRequest(identifier="existing@example.test", password=PASSWORD),
                        timeout=15,
                    )
                    claims = legacy.verify_token(login.token)
                    assert claims["subject_id"] == user.user_id and claims["email"] == user.email
                    assert login.user.zone_id == "fixture-zone" and login.user.last_login_at
                    with sessions() as session:
                        stored = session.get(UserModel, user.user_id)
                        assert stored.updated_at == stored.last_login_at
                    groups.append(
                        "Rust login -> Python JWT verification, original identity and zone"
                    )

                    status, updated = request(
                        base, "/auth/me", login.token, {"display_name": "Rust 更新 ✓"}, "PATCH"
                    )
                    assert status == 200 and updated["display_name"] == "Rust 更新 ✓"
                    with sessions() as session:
                        assert session.get(UserModel, user.user_id).display_name == "Rust 更新 ✓"
                    status, result = request(
                        base,
                        "/auth/change-password",
                        login.token,
                        {"current_password": PASSWORD, "new_password": NEXT_PASSWORD},
                    )
                    assert status == 200 and result["success"]
                    assert legacy.login("existing", PASSWORD) is None
                    assert legacy.login("existing", NEXT_PASSWORD)
                    groups.append(
                        "Rust profile/password writes -> Python reads and bcrypt authentication"
                    )

                    status, _ = request(
                        base, "/auth/me", login.token, {"is_global_admin": True}, "PATCH"
                    )
                    assert status == 422
                    status, _ = request(base, "/auth/me", login.token + "tampered")
                    assert status == 401
                    try:
                        client.GetProfile(pb.GetProfileRequest(), timeout=15)
                    except grpc.RpcError as error:
                        assert error.code() == grpc.StatusCode.UNAUTHENTICATED
                    else:
                        raise AssertionError("Missing account credential was accepted")
                    groups.append("Both transports reject invalid identity and privilege mutation")

                    status, message = request(
                        base, "/auth/verify-email", body={"token": old_verification}
                    )
                    assert status == 200
                    with sessions() as session:
                        verified_at = session.get(UserModel, old_pending.user_id).updated_at
                    client.VerifyEmail(pb.VerifyEmailRequest(token=old_verification), timeout=15)
                    with sessions() as session:
                        assert session.get(UserModel, old_pending.user_id).updated_at == verified_at
                    assert legacy.login("old-pending@example.test", PASSWORD)
                    groups.append(
                        "Python verification link -> Rust HTTP/gRPC, idempotent SQL fact and Python login"
                    )

                    responses = [
                        request(base, "/auth/request-verification", body=body)
                        for body in (
                            {"email": "pending@example.test"},
                            {"email": "missing@example.test"},
                            {"email": "existing@example.test"},
                            {},
                        )
                    ]
                    assert all(
                        result == (202, {"message": "Verification request accepted."})
                        for result in responses
                    )
                    delivered = smtp[1].get(timeout=15)
                    assert str(delivered["To"]) == "pending@example.test"
                    link = re.search(r"https://[^\s]+", delivered.get_content()).group(0)
                    parsed = urlsplit(link)
                    assert (parsed.scheme, parsed.netloc, parsed.path) == (
                        "https",
                        "nexus.example.test",
                        "/verify-email",
                    )
                    verification = parse_qs(parsed.query)["token"][0]
                    assert legacy.verify_email_token(verification) == (
                        pending.user_id,
                        pending.email,
                    )
                    assert request(base, "/auth/me", verification)[0] == 401
                    with sessions.begin() as session:
                        stored = session.get(UserModel, pending.user_id)
                        assert stored.email_verified == 0
                        stored.email = "changed@example.test"
                    assert (
                        request(base, "/auth/verify-email", body={"token": verification})[0] == 400
                    )
                    with sessions.begin() as session:
                        session.get(UserModel, pending.user_id).email = pending.email
                    groups.append(
                        "Rust SMTP message and verification JWT -> Python verifier; stale email and account credential reuse rejected"
                    )

            with server(
                args.binary.resolve(), url, args.output_dir / "accounts-restart.log"
            ) as ready:
                base = f"http://{ready['http_addr']}"
                status, _ = request(base, "/auth/verify-email", body={"token": verification})
                assert status == 200 and legacy.login("pending", PASSWORD)
                assert smtp[1].empty()
                groups.append(
                    "Verification link survives restart with no persisted token or delivery state"
                )

                owners = [
                    legacy.register_user(f"owner-{i}@example.test", PASSWORD) for i in range(2)
                ]
                with sessions.begin() as session:
                    for owner in owners:
                        session.get(UserModel, owner.user_id).email = "collision@example.test"
                tokens = [
                    legacy.create_email_verification_token(owner.user_id, "collision@example.test")
                    for owner in owners
                ]
                with grpc.insecure_channel(ready["grpc_addr"]) as channel:
                    grpc.channel_ready_future(channel).result(timeout=15)
                    client = rpc.AccountsServiceStub(channel)

                    def verify(token):
                        try:
                            client.VerifyEmail(pb.VerifyEmailRequest(token=token), timeout=15)
                            return grpc.StatusCode.OK
                        except grpc.RpcError as error:
                            return error.code()

                    with ThreadPoolExecutor(max_workers=2) as executor:
                        results = list(executor.map(verify, tokens))
                    assert results.count(grpc.StatusCode.OK) == 1
                    assert results.count(grpc.StatusCode.ALREADY_EXISTS) == 1
                with sessions() as session:
                    assert (
                        sum(
                            session.get(UserModel, owner.user_id).email_verified for owner in owners
                        )
                        == 1
                    )
                groups.append(
                    "Concurrent verification keeps one email owner without merging account identities"
                )
                status, account = request(base, "/auth/me", login.token)
                assert status == 200 and account["display_name"] == "Rust 更新 ✓"
                status, session_data = request(
                    base, "/auth/login", body={"identifier": "existing", "password": NEXT_PASSWORD}
                )
                assert status == 200 and session_data["user"]["user_id"] == user.user_id
                with sessions.begin() as session:
                    stored = session.get(UserModel, user.user_id)
                    assert (
                        stored.user_metadata == '{"preserved":true}' and not stored.is_global_admin
                    )
                    stored.is_active = 0
                    session.add(stored)
                status, _ = request(base, "/auth/me", session_data["token"])
                assert status == 401
                groups.append(
                    "Restart preserves account facts; deactivation rejects a previously valid token"
                )
            assert inspect(engine).get_table_names() == ["users"]
            for name in ("accounts-first.log", "accounts-restart.log"):
                output = (args.output_dir / name).read_text(encoding="utf8")
                assert all(
                    token not in output
                    for token in (old_verification, verification, legacy_token, login.token)
                )
            groups.append("No session, token or runtime-state tables were persisted")
        finally:
            engine.dispose()
    result = {"status": "success", "groups": groups, "count": len(groups)}
    (args.output_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
