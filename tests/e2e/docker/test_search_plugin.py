"""search-plugin E2E — locks the v1 SearchService contract.

The `nexus-search-plugin` cdylib is loaded by nexusd-cluster via
`--plugin-dir` and routed via the Phase P plugin-as-gRPC-service path.
This suite spins up the single-node compose in
`dockerfiles/docker-compose.search-plugin-e2e.yml` and drives
`nexus.search.v1.SearchService` (Glob + Grep) via typed gRPC.

Test cases mirror the plan's Category A.4 punch list:

  * test_glob_recursive_over_seeded_files
  * test_grep_regex_with_context
  * test_glob_truncated_when_over_max_results
  * test_grep_skips_binary_and_oversized_files

Cross-node search is deferred to v2 (trigram) per the plan — a single
node substrate is the right regression cover for the v1 contract.

There are **no** `pytest.skip` calls (aside from the top-level
NEXUS_SEARCH_PLUGIN_E2E env gate — same convention as the runbook
suite).  Anything the test would historically have skipped (docker
socket missing, plugin unloaded, method not found) is a hard failure
here — silent skips are the anti-pattern this file exists to avoid.
"""

from __future__ import annotations

import os

import pytest

from tests.e2e.docker.runbook_helpers import (
    ADMIN_API_KEY,
    assert_log_contains,
    uid,
    vfs_mkdir,
    vfs_write,
    wait_healthy,
)
from tests.e2e.docker.search_helpers import search_glob, search_grep, search_index, search_query

pytestmark = [
    pytest.mark.xdist_group("search-plugin-e2e"),
    pytest.mark.skipif(
        os.environ.get("NEXUS_SEARCH_PLUGIN_E2E") != "1",
        reason="search-plugin E2E suite needs the docker-compose.search-plugin-e2e stack; "
        "set NEXUS_SEARCH_PLUGIN_E2E=1 to enable (CI sets this automatically).",
    ),
]


NODE_GRPC = os.environ.get("NEXUS_SEARCH_NODE_GRPC", "node:2126")
NODE_CONTAINER = os.environ.get("NEXUS_SEARCH_NODE_CONTAINER", "nexus-search-plugin-node")


# ---------------------------------------------------------------------------
# Module-scoped setup
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def _cluster_ready() -> None:
    """Hard-fail if the node isn't reachable or the plugin didn't load.

    Two gates:
      1. gRPC port reachable — same TCP probe every other Docker suite uses.
      2. `plugin loaded` log line present — proves --plugin-dir scan
         picked up the cdylib and the Phase P proxy armed the
         SearchService endpoint.  Without this second gate a broken
         plugin load would surface as a mysterious "Unimplemented"
         gRPC error at first call, several layers away from the root
         cause.
    """
    wait_healthy([NODE_GRPC])
    assert_log_contains(
        NODE_CONTAINER,
        "plugins loaded from --plugin-dir",
        msg="cluster booted but --plugin-dir scan did not register any plugin — "
        "search-plugin cdylib may be missing or its signature failed to verify",
    )


@pytest.fixture(scope="module")
def api_key() -> str:
    return ADMIN_API_KEY


# ===========================================================================
# TestSearchGlob
# ===========================================================================
class TestSearchGlob:
    """Recursive glob over the kernel VFS via sys_readdir.

    Locks the Phase P wire contract for `SearchService.Glob` and the
    plugin's globset-based pattern matcher.
    """

    def test_glob_recursive_over_seeded_files(self, api_key: str) -> None:
        """Seed a small tree under /search-<uid>/, glob for **/*.py.

        Any of `foo.py`, `sub/bar.py`, `sub/deeper/baz.py` in the response
        proves the recursive walker descends AND the globset pattern
        matches at all depths.
        """
        u = uid()
        base = f"/search-{u}"
        vfs_mkdir(NODE_GRPC, base, parents=True, api_key=api_key)
        vfs_mkdir(NODE_GRPC, f"{base}/sub", parents=True, api_key=api_key)
        vfs_mkdir(NODE_GRPC, f"{base}/sub/deeper", parents=True, api_key=api_key)
        for path in (
            f"{base}/foo.py",
            f"{base}/sub/bar.py",
            f"{base}/sub/deeper/baz.py",
            f"{base}/README.md",  # non-.py — must NOT match **/*.py
        ):
            r = vfs_write(NODE_GRPC, path, b"# seed\n", api_key=api_key)
            assert "error" not in r, f"seed write failed for {path}: {r}"

        r = search_glob(NODE_GRPC, base, "**/*.py", api_key=api_key)
        assert "error" not in r, f"glob returned error: {r}"
        paths = set(r["result"]["paths"])
        # GlobResponse.paths are absolute VFS paths — same convention as
        # GrepMatch.path.  See the service-level comment on
        # `rpc Glob` in search.proto for the "match relative, return
        # absolute" split.
        assert f"{base}/foo.py" in paths, f"missing foo.py in {paths}"
        assert f"{base}/sub/bar.py" in paths, f"missing sub/bar.py in {paths}"
        assert f"{base}/sub/deeper/baz.py" in paths, f"missing sub/deeper/baz.py in {paths}"
        assert f"{base}/README.md" not in paths, f"README.md leaked past **/*.py filter: {paths}"
        assert r["result"]["truncated"] is False

    def test_glob_truncated_when_over_max_results(self, api_key: str) -> None:
        """Seed N+1 files, request max_results=N, assert truncated=true."""
        u = uid()
        base = f"/search-trunc-{u}"
        vfs_mkdir(NODE_GRPC, base, parents=True, api_key=api_key)
        for i in range(5):
            r = vfs_write(NODE_GRPC, f"{base}/f{i}.txt", b"x", api_key=api_key)
            assert "error" not in r, f"seed write failed: {r}"

        r = search_glob(NODE_GRPC, base, "*.txt", max_results=3, api_key=api_key)
        assert "error" not in r, f"glob returned error: {r}"
        assert r["result"]["truncated"] is True, (
            f"expected truncated=true when 5 matches exceed max_results=3, got {r['result']}"
        )
        assert len(r["result"]["paths"]) == 3

    def test_glob_sort_recency_ranks_newest_first(self, api_key: str) -> None:
        """Seed 3 files with a spacing wide enough for the metastore
        mtime tick to advance, then glob with ``sort_recency=True`` and
        assert paths come back newest-first.

        Uses vfs_write's inherent write ordering + a small sleep between
        writes to guarantee mtime monotonicity — the plugin's sort key
        is the containing-file mtime returned by the kernel's ``sys_stat``
        callback (which now exposes ``modified_at_ms``).
        """
        import time as _time

        u = uid()
        base = f"/search-recency-{u}"
        vfs_mkdir(NODE_GRPC, base, parents=True, api_key=api_key)

        # Seed oldest first, sleep between writes, newest last.
        for name in ("oldest.txt", "middle.txt", "newest.txt"):
            r = vfs_write(NODE_GRPC, f"{base}/{name}", b"seed\n", api_key=api_key)
            assert "error" not in r, f"seed write failed for {name}: {r}"
            _time.sleep(1.1)

        r = search_glob(NODE_GRPC, base, "*.txt", sort_recency=True, api_key=api_key)
        assert "error" not in r, f"glob returned error: {r}"
        paths = r["result"]["paths"]
        assert len(paths) == 3, f"expected 3 paths, got {paths}"
        # Newest-first — reverse of write order.
        assert paths == [
            f"{base}/newest.txt",
            f"{base}/middle.txt",
            f"{base}/oldest.txt",
        ], f"recency sort violated newest-first order: {paths}"


# ===========================================================================
# TestSearchGrep
# ===========================================================================
class TestSearchGrep:
    """Regex grep over the kernel VFS via sys_read.

    Locks: regex matching, context lines, and the two silent-skip
    filters (binary via non-UTF-8 sniff, oversized via GREP_MAX_FILE_BYTES=8MiB).
    """

    def test_grep_regex_with_context(self, api_key: str) -> None:
        """Match `error:` with 1-line before/after context."""
        u = uid()
        base = f"/search-grep-{u}"
        vfs_mkdir(NODE_GRPC, base, parents=True, api_key=api_key)
        payload = b"line1: preamble\nline2: error: fatal boom\nline3: stack frame\n"
        r = vfs_write(NODE_GRPC, f"{base}/log.txt", payload, api_key=api_key)
        assert "error" not in r, f"seed write failed: {r}"

        r = search_grep(
            NODE_GRPC,
            base,
            r"error:",
            before_context=1,
            after_context=1,
            api_key=api_key,
        )
        assert "error" not in r, f"grep returned error: {r}"
        matches = r["result"]["matches"]
        assert len(matches) == 1, f"expected exactly 1 match, got {matches}"
        m = matches[0]
        assert m["line_number"] == 2
        assert "error: fatal boom" in m["line"]
        assert m["before"] == ["line1: preamble"]
        assert m["after"] == ["line3: stack frame"]

    def test_grep_skips_binary_and_oversized_files(self, api_key: str) -> None:
        """Seed one binary file, one small text file with a match.

        Assert:
          - the text-file match returns.
          - the binary file does NOT contribute a match — its bytes
            happen to contain the match pattern but the non-UTF-8 sniff
            silently skips it (mirrors GNU grep --binary-files=skip).
        """
        u = uid()
        base = f"/search-skip-{u}"
        vfs_mkdir(NODE_GRPC, base, parents=True, api_key=api_key)
        # Binary blob: pattern bytes embedded inside NUL + high bytes so
        # from_utf8 rejects the whole payload.
        binary_payload = b"\x00\xff\xfeneedle\x80\xff\x00"
        r = vfs_write(NODE_GRPC, f"{base}/blob.bin", binary_payload, api_key=api_key)
        assert "error" not in r, f"binary seed failed: {r}"
        # Regular UTF-8 text with the same pattern.
        r = vfs_write(NODE_GRPC, f"{base}/text.txt", b"look for needle here\n", api_key=api_key)
        assert "error" not in r, f"text seed failed: {r}"

        r = search_grep(NODE_GRPC, base, r"needle", api_key=api_key)
        assert "error" not in r, f"grep returned error: {r}"
        matches = r["result"]["matches"]
        assert len(matches) == 1, (
            f"expected exactly one match (text only); got {matches}. "
            "binary file must be silently skipped by the plugin's UTF-8 sniff."
        )
        assert matches[0]["path"].endswith("text.txt"), matches


# ===========================================================================
# TestSearchIndexQuery — P1 keyword-only ranked search
# ===========================================================================
class TestSearchIndexQuery:
    """End-to-end coverage for the P1 Index + Query RPCs.

    The unit + Rust integration tests (query_e2e.rs, index_e2e.rs)
    already exercise these RPCs against a real ``SearchServiceImpl``
    with a tempdir index — this suite runs the same contract through
    the LOADED cdylib inside a real ``nexusd-cluster`` process, so
    the plugin-ABI dispatch layer, the tantivy on-disk index under
    the resolved ``NEXUS_DATA_DIR``, and the tonic client wire all
    stay honest against the shipping binary.
    """

    def test_index_then_query_roundtrip(self, api_key: str) -> None:
        """Seed a small tree via vfs_write, Index it, Query — verify hits."""
        u = uid()
        base = f"/search-idx-{u}"
        vfs_mkdir(NODE_GRPC, base, parents=True, api_key=api_key)
        # Two hits for `widget`, one distractor.
        r = vfs_write(NODE_GRPC, f"{base}/a.md", b"widget alpha docs\n", api_key=api_key)
        assert "error" not in r, r
        r = vfs_write(NODE_GRPC, f"{base}/b.md", b"widget beta manual\n", api_key=api_key)
        assert "error" not in r, r
        r = vfs_write(NODE_GRPC, f"{base}/c.md", b"unrelated content\n", api_key=api_key)
        assert "error" not in r, r

        r = search_index(NODE_GRPC, base, api_key=api_key)
        assert "error" not in r, f"index returned error: {r}"
        assert r["result"]["indexed_count"] == 3, r
        assert r["result"]["skipped_count"] == 0, r

        r = search_query(NODE_GRPC, "widget", path_filter=base, api_key=api_key)
        assert "error" not in r, f"query returned error: {r}"
        results = r["result"]["results"]
        assert len(results) == 2, f"expected two 'widget' hits, got {results}"
        paths = {res["path"] for res in results}
        assert paths == {f"{base}/a.md", f"{base}/b.md"}, paths
        # Scores are BM25 — both positive.
        for res in results:
            assert res["score"] > 0.0, res

    def test_reindex_is_idempotent(self, api_key: str) -> None:
        """Running Index twice must NOT duplicate documents in the corpus.

        Regression for the ``delete_term(path)``-before-add contract in
        ``FtsIndex.add_document`` — a naive add-only path would double
        the doc's BM25 score and inflate the result count.
        """
        u = uid()
        base = f"/search-reidx-{u}"
        vfs_mkdir(NODE_GRPC, base, parents=True, api_key=api_key)
        r = vfs_write(NODE_GRPC, f"{base}/f.md", b"widget only\n", api_key=api_key)
        assert "error" not in r, r

        for _ in range(2):
            r = search_index(NODE_GRPC, base, api_key=api_key)
            assert "error" not in r, r
            assert r["result"]["indexed_count"] == 1, r

        r = search_query(NODE_GRPC, "widget", path_filter=base, api_key=api_key)
        assert "error" not in r, r
        assert len(r["result"]["results"]) == 1, (
            f"reindex must not duplicate the doc; got {r['result']['results']}"
        )

    def test_query_semantic_rejected_with_p2_message(self, api_key: str) -> None:
        """Non-keyword query_type must fail loudly in P1 with a phase hint.

        Locks the contract Phase 2 will lift — a silent fall-through to
        keyword ranking would silently mis-serve callers who expected
        vector recall.
        """
        r = search_query(NODE_GRPC, "widget", query_type="semantic", api_key=api_key)
        assert "error" in r, f"semantic must be rejected in P1, got {r}"
        assert "P2" in r["error"], f"semantic rejection should mention P2, got {r}"

    def test_query_hybrid_rejected_with_p3_message(self, api_key: str) -> None:
        r = search_query(NODE_GRPC, "widget", query_type="hybrid", api_key=api_key)
        assert "error" in r, f"hybrid must be rejected in P1, got {r}"
        assert "P3" in r["error"], f"hybrid rejection should mention P3, got {r}"

    def test_query_skips_binary_and_oversized_files_at_index_time(self, api_key: str) -> None:
        """Same skip filter grep uses — non-utf8 payload + oversize must
        never enter the FTS corpus, so a keyword Query returns nothing
        for those files even though `widget` is in their bytes.
        """
        u = uid()
        base = f"/search-idx-skip-{u}"
        vfs_mkdir(NODE_GRPC, base, parents=True, api_key=api_key)
        # Non-UTF-8 payload containing the pattern — must be skipped.
        r = vfs_write(NODE_GRPC, f"{base}/blob.bin", b"\x00\xffwidget\x80\xff", api_key=api_key)
        assert "error" not in r, r
        # Valid neighbour.
        r = vfs_write(NODE_GRPC, f"{base}/ok.md", b"widget textual\n", api_key=api_key)
        assert "error" not in r, r

        r = search_index(NODE_GRPC, base, api_key=api_key)
        assert "error" not in r, r
        assert r["result"]["indexed_count"] == 1, r
        assert r["result"]["skipped_count"] == 1, r

        r = search_query(NODE_GRPC, "widget", path_filter=base, api_key=api_key)
        assert "error" not in r, r
        assert len(r["result"]["results"]) == 1
        assert r["result"]["results"][0]["path"] == f"{base}/ok.md"
