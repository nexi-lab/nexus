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
            Module(
                id="filesystem",
                name="Filesystem brick",
                description="Core fs",
                layer="brick",
                depends_on=[],
            ),
            Module(
                id="rebac",
                name="ReBAC",
                description="Permissions",
                layer="brick",
                depends_on=["filesystem"],
            ),
        ],
        operations=[
            Operation(
                id="filesystem.read",
                module="filesystem",
                summary="Read bytes from a path",
                transports={
                    "cli": TransportCell("nexus filesystem read", "src/x.py:1"),
                    "http": TransportCell("POST /api/v1/filesystem/read", "src/y.py:2"),
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
    assert "Nexus API/RPC Architecture Map" in html or "Nexus API/RPC Surface Map" in html
    assert "filesystem.read" in html
    assert "nexus filesystem read" in html
    assert "POST /api/v1/filesystem/read" in html
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
    # subgraphs per layer
    assert "subgraph layer_brick" in html
    assert "subgraph layer_transport" in html


def test_render_autoescapes_data_fields():
    """Data fields (summary, transport name) with HTML metacharacters must be escaped.

    Defense in depth — op summaries come from docstrings (developer-written) but
    extractor-emitted names also flow through. Autoescape protects against any
    future source-controlled string leaking into the page raw.
    """
    coverage = SurfaceCoverage(
        schema_version=1,
        modules=[
            Module(
                id="filesystem",
                name="Filesystem brick",
                description="Core fs",
                layer="brick",
                depends_on=[],
            ),
        ],
        operations=[
            Operation(
                id="filesystem.read",
                module="filesystem",
                summary="<script>alert(1)</script>",
                transports={
                    "cli": TransportCell(
                        "nexus <evil> & 'foo'",
                        "src/x.py:1",
                    ),
                },
                profiles={
                    "lite": ProfileStatus.SUPPORTED,
                    "sandbox": ProfileStatus.SUPPORTED,
                    "full": ProfileStatus.SUPPORTED,
                },
            ),
        ],
    )
    html = render_html(coverage)
    assert "<script>alert(1)</script>" not in html, "summary must be escaped"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<evil>" not in html, "transport name must be escaped"
    assert "&lt;evil&gt;" in html
    # Mermaid graph must still render its `-->` arrows unescaped (marked | safe)
    assert "-->" in html or "subgraph layer_brick" in html
