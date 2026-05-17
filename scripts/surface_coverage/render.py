"""Render SurfaceCoverage to HTML via jinja2."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import jinja2

from scripts.surface_coverage.schema import SurfaceCoverage

_TEMPLATE_DIR = Path(__file__).parent / "templates"

TRANSPORT_DISPLAY = [
    ("cli", "CLI"),
    ("grpc_typed", "RPC"),
    ("grpc_call", "Call"),
    ("grpc_expose", "expose"),
    ("http", "HTTP"),
    ("mcp", "MCP"),
    ("sdk", "SDK"),
]


def _load_mermaid_js() -> str:
    """Return inline Mermaid runtime.

    v1 uses a CDN script tag inline; Task 15 replaces this with a vendored copy
    under docs/architecture/_vendor/mermaid.min.js when present.
    """
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
    return tmpl.render(
        modules=coverage.modules,
        ops_by_module=ops_by_module,
        transport_display=TRANSPORT_DISPLAY,
        mermaid_js=_load_mermaid_js(),
    )
