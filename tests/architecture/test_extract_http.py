"""HTTP extractor: AST-scan FastAPI decorators."""

from pathlib import Path

from scripts.surface_coverage.extract_http import extract_http_routes


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
