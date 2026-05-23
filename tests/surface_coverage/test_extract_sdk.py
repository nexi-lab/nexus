"""SDK extractor: enumerate public methods on remote client classes."""

from pathlib import Path

from scripts.surface_coverage.extract_sdk import extract_sdk_methods


def test_extract_sdk_from_fixture(tmp_path: Path):
    f = tmp_path / "base_client.py"
    f.write_text(
        "class BaseRemoteClient:\n"
        "    def read(self, path): pass\n"
        "    def write(self, path, data): pass\n"
        "    def _private(self): pass\n"
        "    async def rebac_grant(self, *args): pass\n"
    )
    results = extract_sdk_methods(f, class_names=("BaseRemoteClient",))
    names = {r.method_name for r in results}
    assert names == {"read", "write", "rebac_grant"}  # _private excluded


def test_extract_sdk_real_file_smoke(repo_root: Path):
    real = repo_root / "src/nexus/remote/base_client.py"
    if not real.exists():
        return
    results = extract_sdk_methods(real, class_names=("BaseRemoteNexusFS",))
    # BaseRemoteNexusFS exposes properties (zone_id, agent_id, etc.) extracted as methods
    assert len(results) > 0, "BaseRemoteNexusFS should expose at least one public member"


def test_extract_sdk_recursive_real_tree_smoke(repo_root: Path):
    """v3: recursive walk of remote/ should find many SDK methods."""
    real = repo_root / "src/nexus/remote"
    if not real.exists():
        return
    results = extract_sdk_methods(real)
    assert len(results) >= 20, f"expected many SDK methods, got {len(results)}"
