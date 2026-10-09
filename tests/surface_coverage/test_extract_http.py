"""HTTP extractor: AST-scan FastAPI decorators."""

from pathlib import Path

import pytest

from scripts.surface_coverage.extract_http import extract_http_routes


def test_included_router_prefixes_aliases_and_multiple_mounts(tmp_path: Path):
    (tmp_path / "child.py").write_text(
        "from fastapi import APIRouter\n"
        "leaf = APIRouter(prefix='/leaf')\n"
        "@leaf.post('/query')\n"
        "async def query(): pass\n"
    )
    (tmp_path / "middle.py").write_text(
        "from fastapi import APIRouter\n"
        "from .child import leaf as child\n"
        "router = APIRouter(prefix='/middle')\n"
        "router.include_router(child, prefix='/extra')\n"
    )
    (tmp_path / "parent.py").write_text(
        "from fastapi import APIRouter\n"
        "from .middle import router as nested\n"
        "api = APIRouter(prefix='/api/v2')\n"
        "api.include_router(nested, prefix='/search')\n"
        "api.include_router(nested, prefix='/other')\n"
    )
    routes = extract_http_routes(tmp_path)
    assert {(r.method, r.path) for r in routes} == {
        ("POST", "/api/v2/search/middle/extra/leaf/query"),
        ("POST", "/api/v2/other/middle/extra/leaf/query"),
    }
    assert all(r.source == f"{tmp_path / 'child.py'}:3" for r in routes)


def test_router_include_cycle_fails_explicitly(tmp_path: Path):
    (tmp_path / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "a = APIRouter()\nb = APIRouter()\n"
        "a.include_router(b)\nb.include_router(a)\n"
        "@a.get('/x')\ndef x(): pass\n"
    )
    with pytest.raises(ValueError, match="Router include cycle"):
        extract_http_routes(tmp_path)


def test_extract_http_from_fixture(tmp_path: Path):
    f = tmp_path / "server.py"
    f.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "@router.get('/api/v1/fs/read')\n"
        "async def read(): pass\n"
        "\n"
        "@router.post('/api/v1/fs/write')\n"
        "async def write(): pass\n"
        "\n"
        "@router.delete('/api/v1/fs/{path}')\n"
        "async def delete_(): pass\n"
    )
    results = extract_http_routes(f)
    routes = {(r.method, r.path) for r in results}
    assert routes == {
        ("GET", "/api/v1/fs/read"),
        ("POST", "/api/v1/fs/write"),
        ("DELETE", "/api/v1/fs/{path}"),
    }


def test_extract_http_real_file_smoke(repo_root: Path):
    real = repo_root / "src/nexus/server/fastapi_server.py"
    if not real.exists():
        return
    results = extract_http_routes(real)
    # fastapi_server.py has a handful of direct decorators (dashboard, debug)
    assert len(results) > 0, "fastapi_server.py should expose at least one direct route"


def test_extract_http_recursive_real_tree_smoke(repo_root: Path):
    """v3: recursive scan should find many more routes than just fastapi_server.py."""
    real_routers = repo_root / "src/nexus/server/api"
    if not real_routers.exists():
        return
    results = extract_http_routes(real_routers)
    assert len(results) >= 50, f"expected many HTTP routes from recursive scan, got {len(results)}"
