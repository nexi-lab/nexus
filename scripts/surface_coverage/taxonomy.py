"""Curated module taxonomy + op-id classification rules.

Replaces the v1 auto-discovery that produced 63 garbage modules. v2 has a
fixed set of ~15 real modules organized into ~7 categories, and every op-id
is classified into exactly one module via the ordered rule list below.

Ops that don't match any rule land in `kernel` (catch-all syscall-like surface)
rather than spawning a new module.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CuratedModule:
    id: str
    name: str
    description: str
    depends_on: tuple[str, ...] = ()


# Order matters in some categories for visual placement, not semantics.
MODULES: list[CuratedModule] = [
    # Data plane — core read/write/metadata
    CuratedModule(
        "fs", "Filesystem", "Read, write, stat, list, copy, move, delete on virtual paths."
    ),
    CuratedModule(
        "kernel", "Kernel", "Low-level syscalls and uncategorized surfaces.", depends_on=("fs",)
    ),
    # Access control
    CuratedModule("rebac", "ReBAC", "Permissions and access control.", depends_on=("fs",)),
    CuratedModule(
        "share_link",
        "Share Links",
        "Public/scoped share URLs for files and folders.",
        depends_on=("rebac",),
    ),
    CuratedModule(
        "oauth", "OAuth", "Credentials, providers, token lifecycle.", depends_on=("rebac",)
    ),
    # Discovery
    CuratedModule("search", "Search", "BM25 / sqlite-vec / hybrid retrieval.", depends_on=("fs",)),
    CuratedModule(
        "semantic",
        "Semantic Index",
        "Embedding-backed lookups and reranking.",
        depends_on=("search",),
    ),
    # Workspaces
    CuratedModule(
        "workspace",
        "Workspaces",
        "Local + remote workspaces, federation roots.",
        depends_on=("fs", "rebac"),
    ),
    CuratedModule(
        "snapshot",
        "Snapshots & Versions",
        "Point-in-time captures, version history.",
        depends_on=("workspace",),
    ),
    # Federation & storage
    CuratedModule("mounts", "Mounts", "Volume drivers, path mappings.", depends_on=("fs",)),
    CuratedModule(
        "connectors",
        "Connectors",
        "External system bridges (GDrive, SharePoint, etc.).",
        depends_on=("mounts", "oauth"),
    ),
    # Agent runtime
    CuratedModule(
        "mcp",
        "MCP",
        "Model Context Protocol tools for agents.",
        depends_on=("fs", "rebac", "search"),
    ),
    CuratedModule(
        "agent", "Agents", "Agent identity, registry, observability.", depends_on=("rebac",)
    ),
    # Admin & ops
    CuratedModule("admin", "Admin", "Bootstrap, user provisioning, status, configuration."),
    CuratedModule("audit", "Audit", "Activity logs, event sourcing.", depends_on=("rebac",)),
    CuratedModule("events", "Events", "Pub/sub, notifications.", depends_on=("audit",)),
    CuratedModule(
        "governance", "Governance", "Policy, compliance, retention.", depends_on=("rebac", "audit")
    ),
    CuratedModule("pay", "Pay", "Billing, quotas, plan enforcement.", depends_on=("rebac",)),
]

# Display order = category buckets. Each category lists module ids in display order.
CATEGORIES: dict[str, list[str]] = {
    "Data plane": ["fs", "kernel"],
    "Access control": ["rebac", "share_link", "oauth"],
    "Discovery": ["search", "semantic"],
    "Workspaces": ["workspace", "snapshot"],
    "Federation & storage": ["mounts", "connectors"],
    "Agent runtime": ["mcp", "agent"],
    "Admin & ops": ["admin", "audit", "events", "governance", "pay"],
}

_MODULES_BY_ID: dict[str, CuratedModule] = {m.id: m for m in MODULES}


def all_module_ids() -> set[str]:
    return set(_MODULES_BY_ID)


def get_module(module_id: str) -> CuratedModule:
    return _MODULES_BY_ID[module_id]


def classify_op_id(op_id: str) -> str:
    """Return the curated module-id for an op-id. Ops without a clear home -> 'kernel'."""
    # Try canonical "module.verb" prefix first.
    head = op_id.split(".", 1)[0]
    if head in _MODULES_BY_ID:
        return head

    # Substring/prefix heuristics on the full id (lowercased).
    lid = op_id.lower()
    rules: list[tuple[str, str]] = [
        # (substring or prefix, module id) — first match wins
        ("share_link", "share_link"),
        ("share.", "share_link"),
        ("sharelink", "share_link"),
        ("oauth", "oauth"),
        ("rebac", "rebac"),
        ("permission", "rebac"),
        ("namespace", "rebac"),
        ("workspace", "workspace"),
        ("snapshot", "snapshot"),
        ("version", "snapshot"),
        ("connector", "connectors"),
        ("mount", "mounts"),
        ("semantic", "semantic"),
        ("embedding", "semantic"),
        ("rerank", "semantic"),
        ("search", "search"),
        ("grep", "search"),
        ("glob", "search"),
        ("mcp", "mcp"),
        ("agent", "agent"),
        ("audit", "audit"),
        ("event", "events"),
        ("governance", "governance"),
        ("policy", "governance"),
        ("compliance", "governance"),
        ("pay", "pay"),
        ("billing", "pay"),
        ("quota", "pay"),
        ("admin", "admin"),
        ("init", "admin"),
        ("provision", "admin"),
        ("config", "admin"),
        ("fs.", "fs"),
        ("file", "fs"),
        ("read", "fs"),
        ("write", "fs"),
        ("delete", "fs"),
        ("stat", "fs"),
        ("list", "fs"),
        ("rename", "fs"),
        ("copy", "fs"),
        ("move", "fs"),
        ("metadata", "fs"),
        ("xattr", "fs"),
    ]
    for needle, mod in rules:
        if needle in lid:
            return mod
    # Catch-all
    return "kernel"


def module_categories() -> dict[str, list[CuratedModule]]:
    """Return categories with resolved CuratedModule lists in display order."""
    return {cat: [_MODULES_BY_ID[mid] for mid in mids] for cat, mids in CATEGORIES.items()}
