"""Live CLI -> Rust HTTP -> signed Search plugin, without Python server RPCs.

Uses the isolated ReBAC/mTLS fixture configured by NEXUS_SEARCH_TEST_HTTP,
NEXUS_SEARCH_TEST_ADMIN_KEY and NEXUS_SEARCH_PLUGIN_TLS_*.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import grpc
import httpx

from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc


async def main() -> None:
    admin = Path(os.environ["NEXUS_SEARCH_TEST_ADMIN_KEY"]).read_text().strip()
    target = os.environ["NEXUS_SEARCH_PLUGIN_TARGET"]
    base = os.environ.get("NEXUS_SEARCH_TEST_HTTP", "http://127.0.0.1:2327")
    tls = grpc.ssl_channel_credentials(
        root_certificates=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CA"]).read_bytes(),
        private_key=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_KEY"]).read_bytes(),
        certificate_chain=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CERT"]).read_bytes(),
    )
    channel = grpc.aio.secure_channel(target, tls)
    vfs = vfs_pb2_grpc.NexusVFSServiceStub(channel)
    suffix = uuid4().hex[:8]
    needle = f"heliotrope{suffix}"
    paths = {name: f"/docs/cli-{name}-{suffix}.txt" for name in ("alice", "bob")}
    keys = {}
    hashes = {}

    def safe(text):
        for secret in (admin, *keys.values()):
            text = text.replace(secret, "[redacted]")
        return text

    async def run(args, key=admin, zone="sharedzone", expected=0, extra_env=None):
        env = os.environ.copy()
        env.update(
            {
                "NEXUS_URL": base,
                "NEXUS_API_KEY": key,
                "NEXUS_ZONE_ID": zone,
                "NEXUS_GRPC_PORT": "1",
                "NEXUS_KERNEL_BINARY": "/search-cli-must-use-http",
                "NEXUS_NO_AUTO_JSON": "1",
                "NO_COLOR": "1",
                "COLUMNS": "240",
            }
        )
        env.update(extra_env or {})
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        output = stdout.decode() + stderr.decode()
        assert process.returncode == expected, (args, process.returncode, safe(output))
        return stdout.decode(), output

    async def cli(args, key=admin, zone="sharedzone", expected=0):
        return await run(["-m", "nexus.cli.main", "search", *args], key, zone, expected)

    async with httpx.AsyncClient(base_url=base, timeout=20, trust_env=False) as rust:

        async def admin_request(method, url, **kwargs):
            response = await rust.request(
                method, url, headers={"Authorization": f"Bearer {admin}"}, **kwargs
            )
            assert response.is_success, (response.status_code, safe(response.text))
            return response.json()

        async def grant(name, method):
            return await admin_request(
                method,
                "/v2/rebac/tuples",
                json={
                    "zone": "sharedzone",
                    "object_type": "file",
                    "object_id": paths[name],
                    "relation": "viewer",
                    "subject_type": "user",
                    "subject_id": f"cli-{name}-{suffix}",
                },
            )

        try:
            for name, path in paths.items():
                minted = await admin_request(
                    "POST",
                    "/v2/auth/keys",
                    json={
                        "subject_type": "user",
                        "subject_id": f"cli-{name}-{suffix}",
                        "zones": ["sharedzone:r"],
                    },
                )
                keys[name], hashes[name] = minted["key"], minted["key_hash"]
                written = await vfs.Write(
                    vfs_pb2.WriteRequest(
                        path=path,
                        content=f"{needle} constellation {name}".encode(),
                        auth_token=admin,
                    )
                )
                assert not written.is_error, safe(str(written))
                await grant(name, "POST")

            # Index through the actual CLI, then consume its committed result.
            _, indexed = await cli(["index", "/docs", "--no-recursive"])
            count = re.search(r"Files indexed:\s*(\d+)", indexed)
            assert count and int(count[1]) >= 2, safe(indexed)
            hits_json, _ = await cli(["query", needle, "--mode", "keyword", "--json"])
            assert {hit["path"] for hit in json.loads(hits_json)} == set(paths.values())
            _, human = await cli(["query", needle, "--mode", "keyword", "--limit", "1"])
            assert "Found 1 results" in human and any(path in human for path in paths.values()), (
                safe(human)
            )
            _, stats_output = await cli(["stats"])
            stats = await admin_request(
                "GET", "/v2/documents/stats", params={"zone_id": "sharedzone"}
            )
            for label, field in (
                ("Indexed files", "fts_path_count"),
                ("Keyword chunks", "fts_doc_count"),
                ("Vector chunks", "ann_chunk_count"),
                ("Pending documents", "pending"),
            ):
                assert f"{label}: {stats[field]}" in stats_output, safe(stats_output)

            # Two real CLI processes use separate user credentials against one node.
            for name, (output, _) in zip(
                paths,
                await asyncio.gather(
                    *(
                        cli(["query", needle, "--mode", "keyword", "--json"], key=keys[name])
                        for name in paths
                    )
                ),
                strict=True,
            ):
                assert [hit["path"] for hit in json.loads(output)] == [paths[name]]
            narrowed, _ = await cli(
                [
                    "query",
                    needle,
                    "--mode",
                    "keyword",
                    "--path",
                    paths["bob"],
                    "--json",
                ],
                key=keys["alice"],
            )
            assert json.loads(narrowed) == []
            await cli(["stats"], key=keys["alice"], expected=77)
            await cli(["index", "/docs"], key=keys["alice"], expected=77)
            await cli(
                ["query", needle, "--mode", "keyword"], key=keys["alice"], zone="root", expected=77
            )

            # The same query must respect live relationship and key revocation.
            await grant("alice", "DELETE")
            revoked_grant, _ = await cli(
                [
                    "query",
                    needle,
                    "--mode",
                    "keyword",
                    "--json",
                ],
                key=keys["alice"],
            )
            assert json.loads(revoked_grant) == []
            await admin_request("DELETE", f"/v2/auth/keys/{hashes['bob']}")
            await cli(["query", needle, "--mode", "keyword"], key=keys["bob"], expected=77)
            await cli(["query", needle, "--mode", "keyword"], key="sk-never-minted", expected=77)

            with tempfile.TemporaryDirectory(prefix="search-benchmark-") as temp_dir:
                dataset = Path(temp_dir)
                corpus = dataset / "corpus"
                corpus.mkdir()
                bench_prefix = f"/docs/benchmark-{suffix}"
                pages = [
                    {
                        "slug": "alpha",
                        "title": f"amaryllis{suffix}",
                        "compiled_truth": "first page",
                    },
                    {
                        "slug": "nested/beta",
                        "title": f"buttercup{suffix}",
                        "compiled_truth": "second page",
                        "timeline": ["one", "two"],
                    },
                ]
                queries = [
                    {"id": str(i), "text": page["title"], "gold": {"relevant": [page["slug"]]}}
                    for i, page in enumerate(pages)
                ]
                for i, page in enumerate(pages):
                    (corpus / f"{i}.json").write_text(json.dumps(page), encoding="utf-8")
                query_file = dataset / "queries.json"
                query_file.write_text(json.dumps(queries), encoding="utf-8")
                result_file = dataset / "results.json"
                bench_env = {
                    "BENCH_CORPUS_DIR": str(corpus),
                    "BENCH_QUERIES_FILE": str(query_file),
                    "BENCH_ZONE_PREFIX": bench_prefix,
                    "NEXUS_GRPC_PORT": target.rsplit(":", 1)[1],
                    "NEXUS_GRPC_TLS": "true",
                    "NEXUS_TLS_CA": os.environ["NEXUS_SEARCH_PLUGIN_TLS_CA"],
                    "NEXUS_TLS_CERT": os.environ["NEXUS_SEARCH_PLUGIN_TLS_CERT"],
                    "NEXUS_TLS_KEY": os.environ["NEXUS_SEARCH_PLUGIN_TLS_KEY"],
                }
                benchmark = [
                    str(Path(__file__).with_name("bench_gbrain_evals.py")),
                    "--mode",
                    "keyword",
                    "--save-results",
                    str(result_file),
                ]
                await run(benchmark, extra_env=bench_env)
                for page in pages:
                    read = await vfs.Read(
                        vfs_pb2.ReadRequest(
                            path=f"{bench_prefix}/{page['slug']}.md", auth_token=admin
                        )
                    )
                    assert not read.is_error and page["title"].encode() in read.content, safe(
                        str(read)
                    )
                metrics = json.loads(result_file.read_text())
                assert metrics["query_type"] == "keyword", metrics
                assert metrics["queries"] == 2 and metrics["hits_any"] == "2/2", metrics
                assert metrics["p_at_5"] == 0.2 and metrics["r_at_5"] == 1.0, metrics
                assert metrics["mrr"] == 1.0, metrics

                # Query-only runs never resolve an upload channel or require admin statistics.
                query_env = {**bench_env, "NEXUS_GRPC_PORT": "invalid"}
                await run([*benchmark, "--skip-index"], extra_env=query_env)
                reused = json.loads(result_file.read_text())
                assert reused["per_query"] == metrics["per_query"], reused
                await run([*benchmark, "--skip-index"], key=keys["alice"], extra_env=query_env)
                assert json.loads(result_file.read_text())["hits_any"] == "0/2"
                await run(
                    [*benchmark, "--skip-index"],
                    key="sk-never-minted",
                    expected=1,
                    extra_env=query_env,
                )
                await run(benchmark, key=keys["alice"], expected=1, extra_env=bench_env)

                # Corpus paths and duplicate slugs fail before any connection or write.
                invalid_page = corpus / "0.json"
                for slug, message in (
                    ("../escape", "Invalid corpus slug"),
                    ("nested/beta", "unique"),
                ):
                    invalid_page.write_text(
                        json.dumps({**pages[0], "slug": slug}), encoding="utf-8"
                    )
                    _, error = await run(benchmark, expected=1, extra_env=query_env)
                    assert message in error, safe(error)
                invalid_page.write_text(json.dumps(pages[0]), encoding="utf-8")
                _, error = await run(
                    benchmark,
                    expected=1,
                    extra_env={**query_env, "BENCH_ZONE_PREFIX": "/docs/../escape"},
                )
                assert "absolute VFS directory" in error, safe(error)
            print(
                "Rust HTTP Search CLI passed: index/query/stats/isolation/path/zone/admin/revocation/benchmark"
            )
        finally:
            await channel.close()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 300))
