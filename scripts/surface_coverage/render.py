"""Render SurfaceCoverage to HTML via jinja2.

Builds the Mermaid graph in Python (avoids jinja whitespace issues) and groups
ops by curated module categories from taxonomy.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import jinja2

from scripts.surface_coverage.schema import SurfaceCoverage
from scripts.surface_coverage.taxonomy import (
    MODULES as TAXONOMY_MODULES,
)
from scripts.surface_coverage.taxonomy import (
    module_categories,
)

_TEMPLATE_DIR = Path(__file__).parent / "templates"

TRANSPORT_DISPLAY = [
    ("cli", "CLI", "cli"),
    ("grpc_typed", "RPC", "rpc"),
    ("grpc_call", "Call", "call"),
    ("grpc_expose", "expose", "expose"),
    ("http", "HTTP", "http"),
    ("mcp", "MCP", "mcp"),
    ("sdk", "SDK", "sdk"),
]


def _build_mermaid(modules) -> str:
    """Render a Mermaid graph string with proper newlines (jinja whitespace
    settings strip newlines from inline loops, so we build it explicitly).
    """
    lines = ["graph LR"]
    by_id = {m.id: m for m in modules}
    # Nodes, grouped via Mermaid subgraphs by category for visual layout.
    from scripts.surface_coverage.taxonomy import CATEGORIES

    for category, ids in CATEGORIES.items():
        # Sanitize subgraph id (no spaces, no special chars)
        sg_id = category.replace(" ", "_").replace("&", "and")
        lines.append(f'  subgraph {sg_id} ["{category}"]')
        for mid in ids:
            if mid in by_id:
                m = by_id[mid]
                lines.append(f'    {m.id}["{m.name}"]')
        lines.append("  end")
    # Edges from depends_on (only edges where both endpoints are in the graph).
    edge_lines = []
    for m in modules:
        for dep in m.depends_on:
            if dep in by_id:
                edge_lines.append(f"  {dep} --> {m.id}")
    edge_lines.sort()
    lines.extend(edge_lines)
    return "\n".join(lines)


def _load_mermaid_js() -> str:
    """Return inline Mermaid runtime, vendored from docs/architecture/_vendor."""
    vendored = Path(__file__).parent.parent.parent / "docs/architecture/_vendor/mermaid.min.js"
    if vendored.exists():
        return f"<script>\n{vendored.read_text()}\n</script>\n"
    return '<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>\n'


def render_html(coverage: SurfaceCoverage) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATE_DIR),
        autoescape=jinja2.select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    tmpl = env.get_template("coverage.html.j2")
    ops_by_module: dict[str, list] = defaultdict(list)
    for op in sorted(coverage.operations, key=lambda o: o.id):
        ops_by_module[op.module].append(op)

    cats = module_categories()
    # Filter out modules with 0 ops to reduce clutter
    cats_visible = {}
    for cat, mods in cats.items():
        visible_mods = [m for m in mods if ops_by_module.get(m.id)]
        if visible_mods:
            cats_visible[cat] = visible_mods

    total_ops = len(coverage.operations)
    total_modules = sum(len(mods) for mods in cats_visible.values())
    total_categories = len(cats_visible)

    return tmpl.render(
        ops_by_module=ops_by_module,
        categories=cats_visible,
        transport_display=TRANSPORT_DISPLAY,
        mermaid_js=_load_mermaid_js(),
        mermaid_graph=_build_mermaid(TAXONOMY_MODULES),
        total_ops=total_ops,
        total_modules=total_modules,
        total_categories=total_categories,
    )
