#!/usr/bin/env python3
"""Run the existing Search acceptance journeys against an isolated native daemon."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "src"), str(REPO)]


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel-binary", type=Path, required=True)
    parser.add_argument("--plugin-library", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--bun", default=shutil.which("bun.exe") or shutil.which("bun"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--model-dir", type=Path)
    mode.add_argument("--keyword-only", action="store_true")
    parser.add_argument("--ort-library", type=Path)
    parser.add_argument(
        "--browser-origin",
        action="append",
        default=[],
        help="Allow this frontend origin in the isolated HTTP fixture (repeatable)",
    )
    parser.add_argument(
        "--frontend-checkout",
        type=Path,
        help="Run the production frontend's browser identity journey; --serve keeps the fixture open",
    )
    parser.add_argument(
        "--serve", action="store_true", help="Keep the prepared fixtures until Ctrl-C"
    )
    args = parser.parse_args()
    for name in ("kernel_binary", "plugin_library"):
        path = getattr(args, name).resolve()
        if not path.is_file():
            parser.error(f"--{name.replace('_', '-')} is not a file")
        setattr(args, name, path)
    if args.model_dir and (not args.ort_library or not args.ort_library.is_file()):
        parser.error("--model-dir requires --ort-library")
    if args.frontend_checkout and "http://127.0.0.1:4173" not in args.browser_origin:
        parser.error("--frontend-checkout requires --browser-origin http://127.0.0.1:4173")
    if not args.bun and not (args.serve or args.frontend_checkout):
        parser.error("Bun is required; provide --bun")
    return args


def ports() -> tuple[int, int]:
    for _ in range(50):
        with socket.socket() as grpc, socket.socket() as peer, socket.socket() as http:
            grpc.bind(("127.0.0.1", 0))
            port = grpc.getsockname()[1]
            if port == 65535:
                continue
            try:
                peer.bind(("127.0.0.1", port + 1))
            except OSError:
                continue
            http.bind(("127.0.0.1", 0))
            return port, http.getsockname()[1]
    raise RuntimeError("No isolated port pair available")


def main() -> int:
    import pytest
    from sqlalchemy import create_engine

    from nexus.remote.rpc_transport import RPCTransport
    from nexus.storage.models.path_context import PathContextModel
    from tests.testkit.kernel import configure_search_plugin

    args = arguments()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir.resolve() / f"run-{time.time_ns()}"
    output.mkdir()
    results: list[dict[str, object]] = []
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("NEXUS_")
        and key not in {"DATABASE_URL", "ORT_DYLIB_PATH", "OPENAI_API_KEY", "OPENROUTER_API_KEY"}
    }
    env.update(
        PYTHONPATH=os.pathsep.join((str(REPO / "src"), str(REPO))),
        NO_PROXY="127.0.0.1,localhost",
        NEXUS_KERNEL_BINARY=str(args.kernel_binary),
        NEXUS_SEARCH_PLUGIN_LIBRARY=str(args.plugin_library),
    )

    def run(
        name: str,
        command: list[str],
        overrides: dict[str, str] | None = None,
        *,
        cwd: Path = REPO,
    ) -> bool:
        print(f"Running {name}", flush=True)
        started = time.monotonic()
        filename = f"{len(results):02d}-{name}.log"
        with (output / filename).open("w", encoding="utf8") as log:
            try:
                result = subprocess.run(
                    command,
                    cwd=cwd,
                    env=env | (overrides or {}),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=300,
                )
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = 124
                log.write("\nAcceptance process exceeded its five-minute budget\n")
        results.append(
            {
                "name": name,
                "exit_code": code,
                "seconds": time.monotonic() - started,
                "log": filename,
            }
        )
        return code == 0

    def script(name: str, *extra: str, overrides: dict[str, str] | None = None) -> bool:
        return run(
            name, [sys.executable, "-X", "utf8", str(REPO / "scripts" / name), *extra], overrides
        )

    try:
        with tempfile.TemporaryDirectory(prefix="fixture-", dir=output) as directory:
            root = Path(directory)
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("NEXUS_KERNEL_BINARY", str(args.kernel_binary))
                patch.setenv("NEXUS_SEARCH_PLUGIN_LIBRARY", str(args.plugin_library))
                configure_search_plugin(root, patch)
                for name in ("NEXUS_PLUGIN_DIR", "NEXUS_LOCAL_TRUSTED_KEYS_DIR"):
                    env[name] = os.environ[name]
            data = root / "data"
            data.mkdir()
            database = root / "path-contexts.db"
            engine = create_engine(f"sqlite:///{database.as_posix()}")
            PathContextModel.__table__.create(engine)
            engine.dispose()
            grpc_port, http_port = ports()
            env.update(
                NEXUS_DATA_DIR=str(data),
                NEXUS_IDENTITY_DIR=str(root / "identity"),
                NEXUS_BIND_ADDR=f"127.0.0.1:{grpc_port}",
                NEXUS_ADVERTISE_ADDR=f"127.0.0.1:{grpc_port}",
                NEXUS_HTTP_ADDR=f"127.0.0.1:{http_port}",
                NEXUS_REBAC_ENABLED="true",
                NEXUS_CLUSTER_INIT="sharedzone",
                NEXUS_CLUSTER_INIT_MOUNTS="/=sharedzone",
                NEXUS_API_KEY_SECRET=secrets.token_hex(32),
                NEXUS_SEARCH_QUERY_EXPANSION="false",
                NEXUS_SEARCH_CONTEXTUAL_CHUNKING="false",
                NEXUS_SEARCH_EMBED_API_URL="",
                DATABASE_URL=f"sqlite:///{database.as_posix()}",
                RUST_LOG="info,h2=warn,hyper=warn,tower=warn,tonic=warn",
            )
            if args.browser_origin:
                env["NEXUS_HTTP_ALLOWED_ORIGINS"] = ",".join(args.browser_origin)
            if args.model_dir:
                env["NEXUS_SEARCH_MODEL_DIR"] = str(args.model_dir.resolve())
                env["ORT_DYLIB_PATH"] = str(args.ort_library.resolve())
                env["PATH"] = (
                    str(args.ort_library.resolve().parent) + os.pathsep + env.get("PATH", "")
                )
            else:
                models = root / "empty-models"
                models.mkdir()
                env["NEXUS_SEARCH_MODEL_DIR"] = str(models)
            daemon_log = output / "daemon.log"
            with daemon_log.open("w", encoding="utf8") as log:
                process = subprocess.Popen(
                    [str(args.kernel_binary), "--plugin-dir", env["NEXUS_PLUGIN_DIR"]],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                prepared: list[Path] = []
                try:
                    deadline = time.monotonic() + 120
                    while "VFS data plane ready" not in daemon_log.read_text(
                        encoding="utf8", errors="replace"
                    ):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError(f"Daemon readiness failed; inspect {daemon_log}")
                        time.sleep(0.1)

                    def mint(kind: str, name: str, *options: str) -> str:
                        result = subprocess.run(
                            [
                                str(args.kernel_binary),
                                "auth",
                                "mint",
                                "--subject-type",
                                kind,
                                "--subject-id",
                                name,
                                *options,
                            ],
                            env=env,
                            capture_output=True,
                            text=True,
                            encoding="utf8",
                            check=True,
                        )
                        return result.stdout.strip()

                    credential = root / "admin-key"
                    credential.write_text(mint("user", "operator", "--admin"), encoding="utf8")
                    env.update(
                        NEXUS_SEARCH_PLUGIN_TARGET=f"127.0.0.1:{grpc_port}",
                        NEXUS_SEARCH_TEST_HTTP=f"http://127.0.0.1:{http_port}",
                        NEXUS_SEARCH_PLUGIN_TLS="true",
                        NEXUS_SEARCH_TEST_ADMIN_KEY=str(credential),
                    )
                    for suffix, filename in (
                        ("CA", "ca.pem"),
                        ("CERT", "node.pem"),
                        ("KEY", "node-key.pem"),
                    ):
                        env[f"NEXUS_SEARCH_PLUGIN_TLS_{suffix}"] = str(data / "tls" / filename)
                        env[f"NEXUS_TLS_{suffix}"] = str(data / "tls" / filename)
                    tls = SimpleNamespace(
                        ca_pem=(data / "tls/ca.pem").read_bytes(),
                        node_cert_pem=(data / "tls/node.pem").read_bytes(),
                        node_key_pem=(data / "tls/node-key.pem").read_bytes(),
                    )
                    transport = RPCTransport(
                        env["NEXUS_SEARCH_PLUGIN_TARGET"],
                        auth_token=credential.read_text(),
                        tls_config=tls,
                    )
                    try:
                        for path in ("/docs", "/workspace"):
                            transport.mkdir(path, parents=True)
                    finally:
                        transport.close()
                    for label in ("a", "b"):
                        bundle = Path(mint("agent", f"search-cert-{label}"))
                        if not bundle.resolve().is_relative_to(root.resolve()):
                            raise RuntimeError("Agent credential escaped the isolated fixture")
                        env[f"NEXUS_SEARCH_TEST_AGENT_{label.upper()}"] = str(bundle)
                    if not args.serve:
                        script("test_search_service_plugin.py")
                        if args.model_dir:
                            script("test_published_search_quality.py")
                        for name in (
                            "test_search_credentials_plugin.py",
                            "test_discovery_service_plugin.py",
                            "test_rust_http_search_cli.py",
                            "test_search_agent_credentials_plugin.py",
                        ):
                            script(name)
                    for name, path in (("keyword", "/docs"), ("hybrid", "/workspace")):
                        if name == "hybrid" and args.keyword_only:
                            continue
                        destination = root / f"{name}.json"
                        if script(
                            "ts_search_fixture.py",
                            "prepare",
                            overrides={
                                "NEXUS_TS_SEARCH_FIXTURE": str(destination),
                                "NEXUS_TS_SEARCH_ROOT": path,
                            },
                        ):
                            prepared.append(destination)
                            key = (
                                "NEXUS_TS_SEARCH_FIXTURE"
                                if name == "keyword"
                                else "NEXUS_TS_SEARCH_HYBRID_FIXTURE"
                            )
                            env[key] = str(destination)
                    if len(prepared) == (1 if args.keyword_only else 2):
                        if args.frontend_checkout:
                            frontend = args.frontend_checkout.resolve()
                            if not run(
                                "native-browser",
                                [
                                    shutil.which("node") or "node",
                                    "tests/native/browser-identity.mjs",
                                    "--http-url",
                                    env["NEXUS_SEARCH_TEST_HTTP"],
                                    "--admin-key-file",
                                    str(credential),
                                    "--output-dir",
                                    str(output / "native-browser"),
                                ],
                                cwd=frontend,
                                overrides=(
                                    {"NEXUS_TEST_CHROME": os.environ["NEXUS_TEST_CHROME"]}
                                    if "NEXUS_TEST_CHROME" in os.environ
                                    else None
                                ),
                            ):
                                raise RuntimeError(
                                    f"Native browser acceptance failed; inspect {output}"
                                )
                        if args.serve:
                            print(
                                f"HTTP: {env['NEXUS_SEARCH_TEST_HTTP']}\ngRPC: {env['NEXUS_SEARCH_PLUGIN_TARGET']}"
                            )
                            print(
                                f"Private credentials and fixtures: {root}\nStop with Ctrl-C.",
                                flush=True,
                            )
                            while True:
                                time.sleep(0.5)
                        elif not args.frontend_checkout:
                            run(
                                "tui",
                                [
                                    args.bun,
                                    "test",
                                    "--timeout",
                                    "30000",
                                    "tests/e2e/native-search.test.tsx",
                                ],
                                cwd=REPO / "packages/nexus-tui",
                            )
                finally:
                    try:
                        for destination in prepared:
                            script(
                                "ts_search_fixture.py",
                                "cleanup",
                                overrides={"NEXUS_TS_SEARCH_FIXTURE": str(destination)},
                            )
                    finally:
                        if process.poll() is None:
                            process.terminate()
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=15)
    except KeyboardInterrupt:
        pass
    finally:
        (output / "results.json").write_text(
            json.dumps(
                {"mode": "keyword" if args.keyword_only else "real-model", "results": results},
                indent=2,
            ),
            encoding="utf8",
        )
        print(f"Evidence: {output}", flush=True)
    return int(any(result["exit_code"] != 0 for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
