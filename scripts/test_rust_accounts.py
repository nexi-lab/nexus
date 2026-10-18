#!/usr/bin/env python3
"""Exercise the Rust account binary against accounts created by the Python implementation."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

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
        with opener.open(req, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


@contextmanager
def server(binary: Path, database_url: str, log_path: Path):
    env = os.environ.copy()
    env["NEXUS_DATABASE_URL"] = database_url
    env["NEXUS_JWT_SECRET"] = SECRET
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
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            process.stdout.close()
            reader.join(timeout=5)


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
    with tempfile.TemporaryDirectory(prefix="nexus-account-acceptance-") as name:
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
            with server(
                args.binary.resolve(), url, args.output_dir / "accounts-first.log"
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

            with server(
                args.binary.resolve(), url, args.output_dir / "accounts-restart.log"
            ) as ready:
                base = f"http://{ready['http_addr']}"
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
