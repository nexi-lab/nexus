"""Render SurfaceCoverage to HTML via jinja2.

v3: layered architecture. Architecture diagram shows 5 layers top-down with
bricks grouped by category inside the brick layer. Mental-model prose section
explains request flow. Per-brick cards show profile gates and op counts per
transport.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import jinja2

from scripts.surface_coverage.schema import SurfaceCoverage
from scripts.surface_coverage.taxonomy import (
    BRICK_CATEGORIES,
    LAYER_LABELS,
    LAYERS,
    bricks_by_category,
    get_module,
    modules_by_layer,
)
from scripts.surface_coverage.taxonomy import (
    MODULES as TAXONOMY_MODULES,
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
    """Layered architecture diagram (top-down): transport → cross → brick → nexus_fs → rust_kernel.

    Bricks are wrapped in a single bricks subgraph (categories shown as
    nested labelled subgraphs inside).
    """
    lines = ["graph TB"]
    by_id = {m.id: m for m in modules}

    for layer in LAYERS:
        layer_label = LAYER_LABELS[layer]
        safe_layer = layer
        lines.append(f'  subgraph layer_{safe_layer} ["{layer_label}"]')
        layer_modules = [m for m in modules if m.layer == layer]
        if layer == "brick":
            # Group bricks by category inside the brick layer
            for category, ids in BRICK_CATEGORIES.items():
                cat_id = "cat_" + category.lower().replace(" ", "_").replace("&", "and")
                cat_modules = [by_id[i] for i in ids if i in by_id]
                if not cat_modules:
                    continue
                lines.append(f'    subgraph {cat_id} ["{category}"]')
                for m in cat_modules:
                    lines.append(f'      {m.id}["{m.name}"]')
                lines.append("    end")
        else:
            for m in layer_modules:
                lines.append(f'    {m.id}["{m.name}"]')
        lines.append("  end")

    edge_lines = []
    for m in modules:
        for dep in m.depends_on:
            if dep in by_id:
                edge_lines.append(f"  {dep} --> {m.id}")
    edge_lines.sort()
    lines.extend(edge_lines)
    return "\n".join(lines)


def _load_mermaid_js() -> str:
    vendored = Path(__file__).parent.parent.parent / "docs/surface-coverage/_vendor/mermaid.min.js"
    if vendored.exists():
        return f"<script>\n{vendored.read_text(encoding='utf-8')}\n</script>\n"
    return '<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>\n'


def _coverage_stats(ops_by_module, all_modules) -> dict:
    """Compute per-module + per-transport coverage counts for the stat bar."""
    by_transport: Counter[str] = Counter()
    for ops in ops_by_module.values():
        for op in ops:
            for t in op.transports:
                by_transport[t] += 1
    return {
        "by_transport": dict(by_transport),
    }


def render_html(coverage: SurfaceCoverage) -> str:
    from scripts.surface_coverage.schema import ProfileStatus as _ProfileStatus

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATE_DIR),
        # NOTE: `.j2` is the actual file extension on our templates. The
        # default `select_autoescape(["html"])` matches by extension and would
        # NOT autoescape `coverage.html.j2` / `_op_row.html.j2`, leaving
        # extractor-supplied strings (op summaries, transport names) rendered
        # raw. Enable autoescape for `j2` too so HTML metacharacters in any
        # data field are safe by default; trusted output uses `| safe`.
        autoescape=jinja2.select_autoescape(enabled_extensions=("html", "j2")),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    tmpl = env.get_template("coverage.html.j2")

    # Separate wishlist ops (all profiles = missing_needed) from real ops
    missing_by_module: dict[str, list] = defaultdict(list)
    ops_by_module: dict[str, list] = defaultdict(list)
    for op in sorted(coverage.operations, key=lambda o: o.id):
        # Skip wishlist ops; they have their own section
        if op.profiles and all(s == _ProfileStatus.MISSING_NEEDED for s in op.profiles.values()):
            missing_by_module[op.module].append(op)
        else:
            ops_by_module[op.module].append(op)
    missing_by_module = dict(sorted(missing_by_module.items()))

    by_layer = modules_by_layer()
    brick_cats = bricks_by_category()
    # Hide modules with zero ops in display (but keep them in the diagram)
    visible_cats: dict[str, list] = {}
    for cat, mods in brick_cats.items():
        active = [m for m in mods if ops_by_module.get(m.id)]
        if active:
            visible_cats[cat] = active

    # Non-brick modules with ops (for sidebar)
    other_visible: dict[str, list] = {}
    for layer in LAYERS:
        if layer == "brick":
            continue
        mods = [m for m in by_layer[layer] if ops_by_module.get(m.id)]
        if mods:
            other_visible[LAYER_LABELS[layer]] = mods

    # Modules present in ops_by_module but absent from the taxonomy display
    # lists (chiefly "uncategorized"). Without this, those ops are invisible
    # in the HTML even though they're counted in total_ops.
    taxonomy_ids = {m.id for m in TAXONOMY_MODULES}
    orphan_module_ids = sorted(set(ops_by_module) - taxonomy_ids)
    orphan_modules = [get_module(mid) for mid in orphan_module_ids]

    stats = _coverage_stats(ops_by_module, TAXONOMY_MODULES)
    total_ops = sum(len(v) for v in ops_by_module.values())

    return tmpl.render(
        ops_by_module=ops_by_module,
        brick_categories=visible_cats,
        other_layers=other_visible,
        layer_labels=LAYER_LABELS,
        transport_display=TRANSPORT_DISPLAY,
        mermaid_js=_load_mermaid_js(),
        mermaid_graph=_build_mermaid(TAXONOMY_MODULES),
        total_ops=total_ops,
        total_bricks=sum(1 for m in TAXONOMY_MODULES if m.layer == "brick"),
        total_transports=sum(1 for m in TAXONOMY_MODULES if m.layer == "transport"),
        coverage_stats=stats,
        get_module=get_module,
        missing_by_module=missing_by_module,
        total_missing=sum(len(v) for v in missing_by_module.values()),
        orphan_modules=orphan_modules,
    )
