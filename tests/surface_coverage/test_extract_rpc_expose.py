"""@rpc_expose extractor: AST-scan src tree for the decorator."""

from pathlib import Path

from scripts.surface_coverage.extract_rpc_expose import extract_rpc_exposes


def test_extract_rpc_expose_from_fixture(tmp_path: Path):
    f = tmp_path / "service.py"
    f.write_text(
        "def rpc_expose(*args, **kwargs):\n"
        "    def deco(fn): return fn\n"
        "    return deco\n"
        "\n"
        "class OAuthService:\n"
        "    @rpc_expose(name='oauth_list_providers', description='...')\n"
        "    def list_providers(self): pass\n"
        "\n"
        "    @rpc_expose(name='oauth_revoke', description='...')\n"
        "    def revoke(self): pass\n"
        "\n"
        "class ShareLinkService:\n"
        "    @rpc_expose(description='Create a share link')\n"
        "    def create_share_link(self): pass\n"
    )
    results = extract_rpc_exposes(tmp_path)
    by_name = {r.name: r for r in results}
    assert "oauth_list_providers" in by_name
    assert "oauth_revoke" in by_name
    # When name= is omitted, fall back to method name
    assert "create_share_link" in by_name


def test_extract_rpc_expose_real_tree_smoke(repo_root: Path):
    real = repo_root / "src/nexus"
    if not real.exists():
        return
    results = extract_rpc_exposes(real)
    # we know oauth_list_providers exists in the repo
    assert any(r.name == "oauth_list_providers" for r in results)
