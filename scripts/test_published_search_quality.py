#!/usr/bin/env python3
"""HERB retrieval acceptance against the published server and Search plugin."""

from __future__ import annotations

import json
import os
import statistics
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from nexus.cli.commands.demo_data import HERB_CORPUS, HERB_QA_SET
from nexus.remote.rpc_transport import RPCTransport


def http(base_url: str, api_key: str, route: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{route}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.load(response)
    if result.get("error"):
        raise RuntimeError(f"Search failed: {result['error']}")
    return result


def verify_quality(base_url: str, api_key: str) -> None:
    if not api_key:
        raise ValueError("NEXUS_API_KEY is required")
    latencies = []
    hits = 0
    for case in HERB_QA_SET:
        started = time.perf_counter()
        body = http(
            base_url,
            api_key,
            "/v2/search/query",
            {
                "q": case["question"],
                "zone_id": "sharedzone",
                "path_filter": "/workspace/demo/herb",
                "query_type": "hybrid",
                "limit": 5,
            },
        )
        latencies.append((time.perf_counter() - started) * 1000)
        found = {result["path"] for result in body["results"]}
        hit = case["expected_file"] in found
        hits += hit
        print(f"HERB {'hit' if hit else 'miss'}: {case['expected_file']}", flush=True)
    if hits < 7:
        raise AssertionError(f"HERB retrieval quality: {hits}/{len(HERB_QA_SET)}, requires 7 hits")
    print(f"HERB retrieval: {hits}/{len(HERB_QA_SET)}; p50={statistics.median(latencies):.1f}ms")


def verify_publication(base_url: str, api_key: str, target: str) -> None:
    tls = SimpleNamespace(
        ca_pem=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CA"]).read_bytes(),
        node_cert_pem=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_CERT"]).read_bytes(),
        node_key_pem=Path(os.environ["NEXUS_SEARCH_PLUGIN_TLS_KEY"]).read_bytes(),
    )
    transport = RPCTransport(target, auth_token=api_key, tls_config=tls, timeout=30)
    sentinel = "/workspace/published-search-contract.txt"
    created = set()
    try:
        transport.ping()
        documents = []
        for path, content, _ in HERB_CORPUS:
            transport.write_file(path, content.encode())
            created.add(path)
            assert transport.read_file(path) == content.encode()
            documents.append({"path": path, "text": content})
        transport.write_file(sentinel, b"zebrafish searchable namespace fact")
        created.add(sentinel)
        documents.append({"path": sentinel, "text": "zebrafish searchable namespace fact"})
        indexed = http(
            base_url,
            api_key,
            "/v2/documents/batch",
            {"documents": documents, "zone_id": "sharedzone"},
        )
        assert indexed["indexed_count"] == len(documents), indexed
        assert indexed["skipped_count"] == 0 and not indexed["skipped_paths"], indexed
        assert indexed["index_seq"] > 0, indexed
        stats = http(base_url, api_key, "/v2/documents/stats?zone_id=sharedzone")
        assert stats["last_index_seq"] >= indexed["index_seq"], stats
        assert stats["pending"] == stats["indexing_in_progress"] == 0, stats
        assert stats["last_successful_index_at_ms"] > 0, stats
        assert stats["fts_path_count"] >= len(documents), stats
        query = {"q": "zebrafish", "query_type": "keyword", "zone_id": "sharedzone"}
        result = http(base_url, api_key, "/v2/search/query", query)
        assert [hit["path"] for hit in result["results"]] == [sentinel], result
        transport.delete_file(sentinel)
        created.remove(sentinel)
        result = http(base_url, api_key, "/v2/search/query", query)
        assert result["results"] == [], result
        verify_quality(base_url, api_key)
    finally:
        try:
            for path in created:
                transport.delete_file(path)
        finally:
            transport.close()


if __name__ == "__main__":
    verify_publication(
        os.environ["NEXUS_SEARCH_TEST_HTTP"],
        Path(os.environ["NEXUS_SEARCH_TEST_ADMIN_KEY"]).read_text().strip(),
        os.environ["NEXUS_SEARCH_PLUGIN_TARGET"],
    )
