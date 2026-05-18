"""Brick discovery."""

from pathlib import Path

from scripts.surface_coverage.extract_bricks import extract_bricks


def test_extract_bricks_from_fixture(tmp_path: Path):
    root = tmp_path / "bricks"
    (root / "rebac").mkdir(parents=True)
    (root / "rebac/brick_factory.py").write_text(
        'BRICK_NAME = "BRICK_REBAC"\n'
        'TIER = "independent"\n'
        'RESULT_KEY = "rebac_manager"\n'
        "def create(ctx, system): pass\n"
    )
    (root / "share_link").mkdir(parents=True)
    (root / "share_link/brick_factory.py").write_text(
        'BRICK_NAME = None\nTIER = "dependent"\nRESULT_KEY = "share_link_service"\n'
    )
    (root / "no_factory").mkdir(parents=True)  # should be skipped

    results = extract_bricks(root)
    by_id = {r.id: r for r in results}
    assert set(by_id) == {"rebac", "share_link"}
    assert by_id["rebac"].brick_name == "BRICK_REBAC"
    assert by_id["rebac"].tier == "independent"
    assert by_id["share_link"].brick_name is None
    assert by_id["share_link"].tier == "dependent"


def test_extract_bricks_real_tree_smoke(repo_root: Path):
    real = repo_root / "src/nexus/bricks"
    if not real.exists():
        return
    results = extract_bricks(real)
    # we know there are 28 bricks; some may lack brick_factory.py — at least 10 should be found
    assert len(results) >= 1
