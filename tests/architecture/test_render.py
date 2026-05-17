"""Renderer: YAML + template -> deterministic HTML."""

from scripts.surface_coverage.render import render_html
from scripts.surface_coverage.schema import (
    Module,
    Operation,
    ProfileStatus,
    SurfaceCoverage,
    TransportCell,
)


def _sample_coverage() -> SurfaceCoverage:
    return SurfaceCoverage(
        schema_version=1,
        modules=[
            Module(id="fs", name="Filesystem", description="Core fs", depends_on=[]),
            Module(id="rebac", name="ReBAC", description="Permissions", depends_on=["fs"]),
        ],
        operations=[
            Operation(
                id="fs.read",
                module="fs",
                summary="Read bytes from a path",
                transports={
                    "cli": TransportCell("nexus fs read", "src/x.py:1"),
                    "http": TransportCell("POST /api/v1/fs/read", "src/y.py:2"),
                },
                profiles={
                    "lite": ProfileStatus.SUPPORTED,
                    "sandbox": ProfileStatus.SUPPORTED,
                    "full": ProfileStatus.SUPPORTED,
                },
            ),
        ],
    )


def test_render_produces_valid_html():
    coverage = _sample_coverage()
    html = render_html(coverage)
    assert "<html" in html
    assert "Nexus API/RPC Surface Map" in html
    assert "fs.read" in html
    assert "nexus fs read" in html
    assert "POST /api/v1/fs/read" in html
    # mermaid block present
    assert "mermaid" in html.lower()


def test_render_is_deterministic():
    coverage = _sample_coverage()
    a = render_html(coverage)
    b = render_html(coverage)
    assert a == b


def test_render_includes_module_graph_edges():
    coverage = _sample_coverage()
    html = render_html(coverage)
    # The taxonomy declares rebac depends_on=["fs"], so edge fs --> rebac must appear
    assert "fs --> rebac" in html
    # Subgraph wrapping by category should be present
    assert "subgraph" in html
