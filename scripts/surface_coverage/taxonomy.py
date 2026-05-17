"""Curated layered architecture taxonomy for Nexus.

Five layers (bottom-up):
    rust_kernel  -> Rust kernel (managed mounts, metadata, cache stores)
    nexus_fs     -> Python NexusFS mixin stack (core/nexus_fs.py)
    brick        -> 28 plugin-style features under src/nexus/bricks/
    cross        -> auth middleware, profile gates
    transport    -> CLI / HTTP / gRPC / MCP / SDK exposures

Bricks are first-class. Each declares its profile gate (e.g. BRICK_REBAC) and
category for visual grouping. Ops classify into the brick (or kernel/transport)
they belong to.
"""

from __future__ import annotations

from dataclasses import dataclass

# layers in display order, top-down
LAYERS = ("transport", "cross", "brick", "nexus_fs", "rust_kernel")
LAYER_LABELS = {
    "transport": "Transports",
    "cross": "Cross-cutting",
    "brick": "Bricks",
    "nexus_fs": "NexusFS wrapper",
    "rust_kernel": "Rust kernel",
}


@dataclass(frozen=True)
class CuratedModule:
    id: str
    name: str
    description: str
    layer: str  # one of LAYERS
    category: str = ""  # within layer, e.g. "access", "discovery"
    brick_gate: str | None = None  # e.g. "BRICK_REBAC" for brick-layer modules
    tier: str | None = None  # "independent" | "dependent" for bricks
    depends_on: tuple[str, ...] = ()


# Brick categories — purely for grouping in UI
BRICK_CATEGORIES: dict[str, list[str]] = {
    "Access control": ["rebac", "auth", "identity", "secrets", "delegation", "access_manifest"],
    "Storage & layout": [
        "filesystem",
        "mount",
        "share_link",
        "workspace",
        "snapshot",
        "versioning",
        "archive",
    ],
    "Discovery": ["search", "discovery", "catalog", "parsers", "context_manifest"],
    "Agent runtime": ["mcp", "sandbox", "agent_log"],
    "Process & policy": ["workflows", "approvals", "task_manager", "governance"],
    "Commerce": ["pay"],
    "Data movement": ["upload", "portability"],
}


# Curated modules — every layer must have at least one entry.
MODULES: list[CuratedModule] = [
    # ── Rust kernel ──
    CuratedModule(
        "rust_kernel",
        "Rust kernel",
        "Mounts, metadata store, cache store. PyO3 FFI surface.",
        layer="rust_kernel",
    ),
    # ── NexusFS wrapper ──
    CuratedModule(
        "nexus_fs",
        "NexusFS",
        "Python mixin stack: Content/Dispatch/Metadata/Watch. Calls Rust kernel.",
        layer="nexus_fs",
        depends_on=("rust_kernel",),
    ),
    # ── 28 bricks ──
    CuratedModule(
        "access_manifest",
        "Access manifest",
        "Pre-computed access decisions for fast path checks.",
        layer="brick",
        category="Access control",
        depends_on=("nexus_fs", "rebac"),
        tier="dependent",
    ),
    CuratedModule(
        "agent_log",
        "Agent log",
        "Append-only agent activity log.",
        layer="brick",
        category="Agent runtime",
        depends_on=("nexus_fs",),
        tier="independent",
    ),
    CuratedModule(
        "approvals",
        "Approvals",
        "Workflow approval gates.",
        layer="brick",
        category="Process & policy",
        depends_on=("rebac", "workflows"),
        tier="dependent",
    ),
    CuratedModule(
        "archive",
        "Archive",
        "Signed zone snapshots / archives.",
        layer="brick",
        category="Storage & layout",
        depends_on=("snapshot", "nexus_fs"),
        tier="dependent",
    ),
    CuratedModule(
        "auth",
        "Auth",
        "OAuth providers + identity tokens.",
        layer="brick",
        category="Access control",
        brick_gate="BRICK_AUTH",
        tier="independent",
    ),
    CuratedModule(
        "catalog",
        "Catalog",
        "Schema extraction (CSV/JSON/Parquet).",
        layer="brick",
        category="Discovery",
        depends_on=("nexus_fs", "parsers"),
        tier="dependent",
    ),
    CuratedModule(
        "context_manifest",
        "Context manifest",
        "Deterministic pre-execution context.",
        layer="brick",
        category="Discovery",
        depends_on=("nexus_fs",),
        tier="dependent",
    ),
    CuratedModule(
        "delegation",
        "Delegation",
        "Agent identity delegation modes.",
        layer="brick",
        category="Access control",
        depends_on=("identity",),
        tier="dependent",
    ),
    CuratedModule(
        "discovery",
        "Discovery",
        "BM25-based tool / surface discovery.",
        layer="brick",
        category="Discovery",
        depends_on=("search",),
        tier="dependent",
    ),
    CuratedModule(
        "filesystem",
        "Filesystem brick",
        "Path-scoping wrappers for NexusFS.",
        layer="brick",
        category="Storage & layout",
        depends_on=("nexus_fs",),
        tier="independent",
    ),
    CuratedModule(
        "governance",
        "Governance",
        "Anti-fraud, policy enforcement.",
        layer="brick",
        category="Process & policy",
        depends_on=("rebac", "agent_log"),
        tier="dependent",
    ),
    CuratedModule(
        "identity",
        "Identity",
        "Identity resolution + subject mapping.",
        layer="brick",
        category="Access control",
        tier="independent",
    ),
    CuratedModule(
        "mcp",
        "MCP",
        "Model Context Protocol tools and provider registry.",
        layer="brick",
        category="Agent runtime",
        depends_on=("nexus_fs", "rebac"),
        tier="dependent",
    ),
    CuratedModule(
        "mount",
        "Mount",
        "Backend mount lifecycle + driver registration.",
        layer="brick",
        category="Storage & layout",
        depends_on=("nexus_fs",),
        tier="independent",
    ),
    CuratedModule(
        "parsers",
        "Parsers",
        "Content parsers (code, docs, structured data).",
        layer="brick",
        category="Discovery",
        tier="independent",
    ),
    CuratedModule(
        "pay",
        "Pay",
        "NexusPay credits + X402 payment rails.",
        layer="brick",
        category="Commerce",
        brick_gate="BRICK_PAY",
        tier="independent",
    ),
    CuratedModule(
        "portability",
        "Portability",
        "Data export/import bundles.",
        layer="brick",
        category="Data movement",
        depends_on=("nexus_fs",),
        tier="dependent",
    ),
    CuratedModule(
        "rebac",
        "ReBAC",
        "Zanzibar-style relation-based access control.",
        layer="brick",
        category="Access control",
        brick_gate="BRICK_REBAC",
        tier="independent",
    ),
    CuratedModule(
        "sandbox",
        "Sandbox",
        "Docker/E2B/Monty code execution.",
        layer="brick",
        category="Agent runtime",
        brick_gate="BRICK_SANDBOX",
        tier="independent",
    ),
    CuratedModule(
        "search",
        "Search",
        "Zoekt / BM25 / semantic search daemon.",
        layer="brick",
        category="Discovery",
        brick_gate="BRICK_SEARCH",
        tier="independent",
    ),
    CuratedModule(
        "secrets",
        "Secrets",
        "Encrypted vault for credentials.",
        layer="brick",
        category="Access control",
        tier="independent",
    ),
    CuratedModule(
        "share_link",
        "Share Link",
        "Public/scoped share URLs for paths.",
        layer="brick",
        category="Storage & layout",
        depends_on=("rebac",),
        tier="dependent",
    ),
    CuratedModule(
        "snapshot",
        "Snapshot",
        "Transactional filesystem snapshots.",
        layer="brick",
        category="Storage & layout",
        depends_on=("nexus_fs",),
        tier="dependent",
    ),
    CuratedModule(
        "task_manager",
        "Task manager",
        "Async task scheduling + tracking.",
        layer="brick",
        category="Process & policy",
        tier="independent",
    ),
    CuratedModule(
        "upload",
        "Upload",
        "TUS / chunked upload handling.",
        layer="brick",
        category="Data movement",
        depends_on=("nexus_fs",),
        tier="dependent",
    ),
    CuratedModule(
        "versioning",
        "Versioning",
        "File history + revert.",
        layer="brick",
        category="Storage & layout",
        depends_on=("nexus_fs",),
        tier="dependent",
    ),
    CuratedModule(
        "workflows",
        "Workflows",
        "Event-driven workflow engine.",
        layer="brick",
        category="Process & policy",
        brick_gate="BRICK_WORKFLOWS",
        depends_on=("task_manager",),
        tier="dependent",
    ),
    CuratedModule(
        "workspace",
        "Workspace",
        "Multi-workspace isolation + federation.",
        layer="brick",
        category="Storage & layout",
        depends_on=("nexus_fs", "rebac"),
        tier="dependent",
    ),
    # ── Cross-cutting ──
    CuratedModule(
        "auth_middleware",
        "Auth middleware",
        "JWT extraction, OperationContext population.",
        layer="cross",
        depends_on=("auth",),
    ),
    CuratedModule(
        "profile_gates",
        "Profile gates",
        "DeploymentProfile-based brick enable/disable.",
        layer="cross",
    ),
    # ── Transports ──
    CuratedModule(
        "cli", "CLI", "nexus command-line interface, lazy command registry.", layer="transport"
    ),
    CuratedModule(
        "http",
        "HTTP",
        "FastAPI routers under server/api/. Per-feature route groups.",
        layer="transport",
    ),
    CuratedModule(
        "grpc",
        "gRPC",
        "NexusVFSService: generic Call dispatch + typed Read/Write/Delete.",
        layer="transport",
    ),
    CuratedModule(
        "mcp_transport",
        "MCP transport",
        "MCP server endpoint wrapping the mcp brick's provider registry.",
        layer="transport",
        depends_on=("mcp",),
    ),
    CuratedModule(
        "sdk", "SDK", "Python remote clients (RPCProxyBase + domain clients).", layer="transport"
    ),
]


# Aliases: surface names that should map to a specific module
# (avoids needing classify_op_id rules for every individual op)
_EXPLICIT_ALIASES: dict[str, str] = {
    # CLI commands often use top-level verbs without a module prefix
    # — these classify under the appropriate brick
    "init": "auth",  # nexus init = bootstrap / auth setup
    "cat": "filesystem",
    "ls": "filesystem",
    "rm": "filesystem",
    "cp": "filesystem",
    "edit": "filesystem",
    "sync": "filesystem",
    "up": "mount",
    "down": "mount",
    "start": "mount",
    "stop": "mount",
    "restart": "mount",
    "rollback": "versioning",
    "connect": "auth",
    "config": "uncategorized",
    "context": "context_manifest",
    "doctor": "uncategorized",
    "info": "uncategorized",
    "logs": "agent_log",
    "network": "uncategorized",
    "run": "sandbox",
    "tls": "auth",
    "upgrade": "uncategorized",
    "migrate": "uncategorized",
    "plugins": "uncategorized",
    "undo": "versioning",
    "llm": "mcp",
    "env": "uncategorized",
    "access": "rebac",
    "is_directory": "filesystem",
    "close": "filesystem",
    "ops": "uncategorized",
}


_MODULES_BY_ID: dict[str, CuratedModule] = {m.id: m for m in MODULES}


# A pseudo-module to collect uncategorized ops without polluting the diagram
_UNCATEGORIZED = CuratedModule(
    id="uncategorized",
    name="Uncategorized",
    description="Operations the classifier couldn't place. Add a rule in taxonomy.py.",
    layer="brick",
    category="(unclassified)",
)


def all_module_ids() -> set[str]:
    return set(_MODULES_BY_ID) | {"uncategorized"}


def get_module(module_id: str) -> CuratedModule:
    if module_id == "uncategorized":
        return _UNCATEGORIZED
    return _MODULES_BY_ID[module_id]


def modules_by_layer() -> dict[str, list[CuratedModule]]:
    """Return modules grouped by layer in LAYERS display order."""
    out: dict[str, list[CuratedModule]] = {layer: [] for layer in LAYERS}
    for m in MODULES:
        out[m.layer].append(m)
    return out


def bricks_by_category() -> dict[str, list[CuratedModule]]:
    """Return brick-layer modules grouped by BRICK_CATEGORIES."""
    by_id = {m.id: m for m in MODULES if m.layer == "brick"}
    out: dict[str, list[CuratedModule]] = {}
    for category, ids in BRICK_CATEGORIES.items():
        cat_mods = [by_id[i] for i in ids if i in by_id]
        if cat_mods:
            out[category] = cat_mods
    return out


# ---------------------------------------------------------------------------
# v2 compatibility shims — render.py still imports these until the view layer
# redesign lands in a later dispatch.  Do NOT remove until render.py is updated.
# ---------------------------------------------------------------------------

# CATEGORIES: maps display-category name -> list of module ids in that category.
# For v3 we expose all brick categories + transport/cross/kernel groups.
CATEGORIES: dict[str, list[str]] = {
    **dict(BRICK_CATEGORIES),
    "Infrastructure": ["rust_kernel", "nexus_fs"],
    "Cross-cutting": ["auth_middleware", "profile_gates"],
    "Transports": ["cli", "http", "grpc", "mcp_transport", "sdk"],
}


def module_categories() -> dict[str, list[CuratedModule]]:
    """v2 compat: return categories with resolved CuratedModule lists."""
    by_id = _MODULES_BY_ID
    out: dict[str, list[CuratedModule]] = {}
    for cat, ids in CATEGORIES.items():
        mods = [by_id[i] for i in ids if i in by_id]
        if mods:
            out[cat] = mods
    return out


def classify_op_id(op_id: str) -> str:
    """Return the curated module-id for an op-id. Uncategorized -> 'uncategorized'."""
    # 1. Exact alias match
    if op_id in _EXPLICIT_ALIASES:
        return _EXPLICIT_ALIASES[op_id]

    # 2. canonical "module.verb" prefix maps directly if the head is a known module
    head = op_id.split(".", 1)[0]
    if head in _MODULES_BY_ID:
        return head

    # 3. Substring/prefix heuristics. Order matters — more specific rules first.
    lid = op_id.lower()
    rules: list[tuple[str, str]] = [
        # --- HTTP-route-derived module prefixes (whole first token matches) ---
        # These appear as "<stem>.<verb>" where the stem is the router file name.
        # More specific patterns first.
        # Verb-only stems (routes in generic routers): route by contained noun
        ("grant.consent", "auth"),
        ("revoke.consent", "auth"),
        ("revoke.key", "auth"),
        ("create.key", "auth"),
        ("update.key", "auth"),
        ("get.key", "auth"),
        ("get.access_logs", "agent_log"),
        ("get.content_id", "filesystem"),
        ("make.private", "rebac"),
        ("make.public", "rebac"),
        ("admin.reindex", "search"),
        ("rpc.api_nfs", "nexus_fs"),
        ("access_manifests.", "access_manifest"),
        ("access_manifest.", "access_manifest"),
        ("nexus_v_f_s_service.", "nexus_fs"),
        ("token_exchange.", "auth"),
        ("x402.", "pay"),
        ("zone_routes.", "workspace"),
        ("zone_meta.", "workspace"),
        ("scheduler.", "task_manager"),
        ("lineage.", "catalog"),
        ("aspects.", "catalog"),
        ("credentials.", "auth"),
        ("credential.", "auth"),
        ("daemon.", "auth"),
        ("admin_bootstrap.", "auth"),
        ("exchange.", "auth"),
        ("federation.", "identity"),
        ("deprovision.", "identity"),
        ("provision.", "identity"),
        ("backfill.", "search"),
        ("reindex.", "search"),
        ("subscriptions.", "workflows"),
        ("subscription.", "workflows"),
        ("locks.", "filesystem"),
        ("lock.", "filesystem"),
        ("exists.", "filesystem"),
        ("batch.", "filesystem"),
        ("cache.", "filesystem"),
        ("stream.", "filesystem"),
        ("graph.", "catalog"),
        ("jwks.", "auth"),
        ("hub.", "uncategorized"),
        ("features.", "uncategorized"),
        ("health.", "uncategorized"),
        ("probes.", "uncategorized"),
        ("operations.", "uncategorized"),
        ("debug.", "uncategorized"),
        ("rpc.", "uncategorized"),
        ("extensions.", "uncategorized"),
        ("admin.", "uncategorized"),
        # CLI bare verbs (no module prefix) — filesystem ops
        # These come through _upsert as bare names before being prefixed.
        # The heuristic: fs-like verbs go to filesystem, others to uncategorized.
        # Bricks (with disambiguation order)
        ("share_link", "share_link"),
        ("sharelink", "share_link"),
        ("oauth", "auth"),
        ("auth_", "auth"),
        ("rebac", "rebac"),
        ("permission", "rebac"),
        ("namespace", "rebac"),
        ("identity", "identity"),
        ("subject", "identity"),
        ("delegation", "delegation"),
        ("delegate", "delegation"),
        ("secret", "secrets"),
        ("vault", "secrets"),
        ("workspace", "workspace"),
        ("snapshot", "snapshot"),
        ("version", "versioning"),
        ("archive", "archive"),
        ("connector", "mount"),
        ("mount", "mount"),
        ("catalog", "catalog"),
        ("schema", "catalog"),
        ("aspect", "catalog"),
        ("parser", "parsers"),
        ("semantic", "search"),
        ("embedding", "search"),
        ("rerank", "search"),
        ("search", "search"),
        ("grep", "search"),
        ("glob", "search"),
        ("discovery", "discovery"),
        ("mcp", "mcp"),
        ("sandbox", "sandbox"),
        ("agent", "agent_log"),
        ("audit", "agent_log"),
        ("event", "agent_log"),
        ("workflow", "workflows"),
        ("approval", "approvals"),
        ("scheduler", "task_manager"),
        ("task", "task_manager"),
        ("governance", "governance"),
        ("policy", "governance"),
        ("compliance", "governance"),
        ("x402", "pay"),
        ("pay", "pay"),
        ("billing", "pay"),
        ("quota", "pay"),
        ("upload", "upload"),
        ("portability", "portability"),
        ("export", "portability"),
        ("import", "portability"),
        ("share", "share_link"),
        # NexusFS / kernel ops (sys_*, fs.*, plain syscalls)
        ("sys_", "nexus_fs"),
        ("sys.", "nexus_fs"),
        ("vfs", "nexus_fs"),
        ("nexus_v_f_s", "nexus_fs"),
        ("fs.", "filesystem"),
        ("file", "filesystem"),
        ("path", "filesystem"),
        ("read", "filesystem"),
        ("write", "filesystem"),
        ("delete", "filesystem"),
        ("stat", "filesystem"),
        ("list", "filesystem"),
        ("rename", "filesystem"),
        ("copy", "filesystem"),
        ("move", "filesystem"),
        ("metadata", "filesystem"),
        ("xattr", "filesystem"),
        ("tree", "filesystem"),
        ("mkdir", "filesystem"),
        ("rmdir", "filesystem"),
        ("append", "filesystem"),
        ("exists", "filesystem"),
        ("lock", "filesystem"),
        ("stream", "filesystem"),
        ("size", "filesystem"),
        ("batch", "filesystem"),
        ("cache", "filesystem"),
        ("backfill", "search"),
        ("reindex", "search"),
        ("lineage", "catalog"),
        # identity / auth patterns
        ("provision", "identity"),
        ("deprovision", "identity"),
        ("federation", "identity"),
        ("credential", "auth"),
        ("daemon", "auth"),
        ("exchange", "auth"),
        ("jwks", "auth"),
        ("bootstrap", "auth"),
        ("token", "auth"),
        ("zone_route", "workspace"),
        ("subscription", "workflows"),
        # Catch-all: ops that remain uncategorized on purpose
        ("health", "uncategorized"),
        ("probe", "uncategorized"),
        ("debug", "uncategorized"),
        ("ops", "uncategorized"),
        ("feature", "uncategorized"),
        ("extension", "uncategorized"),
    ]
    for needle, mod in rules:
        if needle in lid:
            return mod

    return "uncategorized"
