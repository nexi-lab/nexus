"""E2E: lightweight locate() over HERB-style folder structure (Issue #3725).

Validates:
1. locate() returns path hits by folder/filename tokens (BM25-lite, no txtai).
2. locate() scores title matches higher than path-only matches.
3. Zone isolation: locate() never leaks docs across zones.
4. locate() does NOT trigger txtai embedding calls — only the in-memory skeleton
   dict is consulted, so it works even with a bare SearchDaemon() (no config).
5. Content semantic search (txtai) is NOT triggered by locate() and is absent
   when no txtai model is configured.

Seed data mirrors the HERB benchmark corpus layout:
    /workspace/demo/herb/customers/cust-001.md  — Acme Corporation
    /workspace/demo/herb/customers/cust-002.md  — BrightLight Ltd
    /workspace/demo/herb/customers/cust-003.md  — CloudMatrix Inc
    /workspace/demo/herb/employees/emp-001.md   — Sarah Kim
    /workspace/demo/herb/employees/emp-002.md   — Daniel Torres
    /workspace/demo/herb/employees/emp-003.md   — Priya Nair
    /workspace/demo/herb/products/prod-001.md   — NanoSynth
    /workspace/demo/herb/products/prod-002.md   — VaultEdge
    /workspace/demo/herb/products/prod-003.md   — TerraFlow
    /workspace/internal/billing/invoice-q1.md   — Q1 Invoice Summary (other zone)
"""

from __future__ import annotations

import pytest

from nexus.bricks.search.daemon import SearchDaemon
from nexus.contracts.constants import ROOT_ZONE_ID

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ZONE = "root"
OTHER_ZONE = "billing-zone"

HERB_DOCS = [
    # (path_id, virtual_path, title)
    (
        "pid-c001",
        "/workspace/demo/herb/customers/cust-001.md",
        "HERB customer record — Acme Corporation",
    ),
    (
        "pid-c002",
        "/workspace/demo/herb/customers/cust-002.md",
        "HERB customer record — BrightLight Ltd",
    ),
    (
        "pid-c003",
        "/workspace/demo/herb/customers/cust-003.md",
        "HERB customer record — CloudMatrix Inc",
    ),
    ("pid-e001", "/workspace/demo/herb/employees/emp-001.md", "HERB employee record — Sarah Kim"),
    (
        "pid-e002",
        "/workspace/demo/herb/employees/emp-002.md",
        "HERB employee record — Daniel Torres",
    ),
    ("pid-e003", "/workspace/demo/herb/employees/emp-003.md", "HERB employee record — Priya Nair"),
    ("pid-p001", "/workspace/demo/herb/products/prod-001.md", "HERB product record — NanoSynth"),
    ("pid-p002", "/workspace/demo/herb/products/prod-002.md", "HERB product record — VaultEdge"),
    ("pid-p003", "/workspace/demo/herb/products/prod-003.md", "HERB product record — TerraFlow"),
]

OTHER_DOCS = [
    ("pid-b001", "/workspace/internal/billing/invoice-q1.md", "Q1 Invoice Summary"),
]


@pytest.fixture
def daemon() -> SearchDaemon:
    """Bare SearchDaemon — no DB, no txtai config, purely in-memory skeleton index."""
    d = SearchDaemon()
    for path_id, virtual_path, title in HERB_DOCS:
        d.upsert_skeleton_doc(path_id=path_id, virtual_path=virtual_path, title=title, zone_id=ZONE)
    for path_id, virtual_path, title in OTHER_DOCS:
        d.upsert_skeleton_doc(
            path_id=path_id, virtual_path=virtual_path, title=title, zone_id=OTHER_ZONE
        )
    return d


# ---------------------------------------------------------------------------
# 1. Basic path-token locate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locate_herb_folder_returns_all_herb_docs(daemon: SearchDaemon) -> None:
    """Query 'herb' matches the /herb/ path segment → all 9 HERB docs surface."""
    results = await daemon.locate("herb", zone_id=ZONE, limit=20)
    paths = [r["path"] for r in results]

    for _, vp, _ in HERB_DOCS:
        assert vp in paths, f"expected {vp!r} in locate('herb') results"


@pytest.mark.asyncio
async def test_locate_customers_subfolder(daemon: SearchDaemon) -> None:
    """Query 'customers' returns only customer docs, not employees/products."""
    results = await daemon.locate("customers", zone_id=ZONE, limit=20)
    paths = [r["path"] for r in results]

    for _, vp, _ in HERB_DOCS:
        if "/customers/" in vp:
            assert vp in paths, f"customer doc missing: {vp}"
        else:
            assert vp not in paths, f"non-customer doc leaked: {vp}"


@pytest.mark.asyncio
async def test_locate_employees_subfolder(daemon: SearchDaemon) -> None:
    """Query 'employees' returns only employee docs."""
    results = await daemon.locate("employees", zone_id=ZONE, limit=20)
    paths = [r["path"] for r in results]

    for _, vp, _ in HERB_DOCS:
        if "/employees/" in vp:
            assert vp in paths, f"employee doc missing: {vp}"
        else:
            assert vp not in paths, f"non-employee doc leaked: {vp}"


# ---------------------------------------------------------------------------
# 2. Title-weighted scoring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locate_acme_returns_cust001_first(daemon: SearchDaemon) -> None:
    """'acme' only appears in cust-001's title → should be top result."""
    results = await daemon.locate("acme", zone_id=ZONE, limit=5)
    assert results, "expected at least one result"
    assert results[0]["path"] == "/workspace/demo/herb/customers/cust-001.md"


@pytest.mark.asyncio
async def test_locate_sarah_returns_emp001_first(daemon: SearchDaemon) -> None:
    """'sarah' only appears in emp-001's title → top result."""
    results = await daemon.locate("sarah", zone_id=ZONE, limit=5)
    assert results
    assert results[0]["path"] == "/workspace/demo/herb/employees/emp-001.md"


@pytest.mark.asyncio
async def test_locate_title_score_gt_path_score(daemon: SearchDaemon) -> None:
    """Title match (weight 2) outscores pure path match (weight 1).

    'acme' is in cust-001's title but NOT in any path segment, so its score
    should be non-zero and come from the title channel only.
    """
    results = await daemon.locate("acme", zone_id=ZONE, limit=1)
    assert results
    assert results[0]["score"] >= 2.0, "title match should score ≥ 2.0 (weight=2)"


# ---------------------------------------------------------------------------
# 3. Zone isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locate_does_not_leak_across_zones(daemon: SearchDaemon) -> None:
    """HERB docs must not appear when querying from OTHER_ZONE."""
    results = await daemon.locate("herb", zone_id=OTHER_ZONE, limit=20)
    paths = [r["path"] for r in results]
    for _, vp, _ in HERB_DOCS:
        assert vp not in paths, f"zone isolation broken: {vp} leaked into {OTHER_ZONE}"


@pytest.mark.asyncio
async def test_locate_root_zone_does_not_see_billing(daemon: SearchDaemon) -> None:
    """billing invoice must not appear in ZONE query."""
    results = await daemon.locate("invoice", zone_id=ZONE, limit=20)
    paths = [r["path"] for r in results]
    for _, vp, _ in OTHER_DOCS:
        assert vp not in paths, f"zone isolation broken: {vp} leaked into {ZONE}"


# ---------------------------------------------------------------------------
# 4. locate() requires no txtai — works on bare daemon without config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locate_works_without_txtai_config() -> None:
    """SearchDaemon() with no config can locate documents via BM25-lite.

    This proves locate() is independent of txtai embeddings — path+title tokens
    are pure string operations on the in-memory dict.
    """
    daemon_no_cfg = SearchDaemon()  # no DaemonConfig → no DB, no txtai
    daemon_no_cfg.upsert_skeleton_doc(
        path_id="pid-test",
        virtual_path="/workspace/src/auth/login.py",
        title="User login module",
        zone_id=ROOT_ZONE_ID,
    )

    results = await daemon_no_cfg.locate("login", zone_id=ROOT_ZONE_ID, limit=5)
    assert results, "locate() must work without txtai config"
    assert results[0]["path"] == "/workspace/src/auth/login.py"


# ---------------------------------------------------------------------------
# 5. Content semantic search NOT enabled by default (no txtai model config)
# ---------------------------------------------------------------------------


def test_daemon_has_no_embedding_model_by_default() -> None:
    """SearchDaemon() without DaemonConfig has no txtai embedding model wired.

    This verifies the default: lightweight path search is on, but full content
    semantic search (txtai vector embeddings) is NOT triggered unless explicitly
    configured via DaemonConfig(txtai_model=...).
    """
    daemon_no_cfg = SearchDaemon()
    stats = daemon_no_cfg.get_stats()
    # backend should be "none", "legacy" (BM25+SQL), or "bm25" — not txtai/vector
    backend = stats.get("backend", "none")
    assert backend not in ("txtai", "vector", "openai"), (
        f"unexpected embedding backend without config: backend={backend!r}. "
        "Full content semantic search must not be enabled without explicit config."
    )


@pytest.mark.asyncio
async def test_locate_result_has_no_embedding_field(daemon: SearchDaemon) -> None:
    """locate() results must not contain an 'embedding' field — pure BM25-lite output."""
    results = await daemon.locate("herb", zone_id=ZONE, limit=3)
    assert results
    for r in results:
        assert "embedding" not in r, f"locate() leaked embedding field: {r}"
        # Only the documented fields should be present
        assert set(r.keys()) <= {"path", "score", "title"}, f"unexpected keys in result: {r}"
