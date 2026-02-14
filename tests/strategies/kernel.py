"""Hypothesis strategies for kernel component testing (Issue #1303).

Provides bounded, reusable strategies for:
  - Virtual paths and mount points (VFS Router)
  - ReadSet entries and read sets (ReadSet / ReadSetRegistry)
  - Agent requests (Scheduler)
  - Operation contexts (Permissions)
  - Agent info with generation counters (Agent Registry)
  - Kernel events (EventLog)
  - Hook specs and contexts (HookEngine)

All strategies are explicitly bounded to prevent pathological inputs:
  - Path strings: max 255 chars, valid path characters
  - Collections: max 50 entries
  - Numeric ranges: realistic bounds
"""

from __future__ import annotations

from hypothesis import strategies as st

from nexus.core.permissions import OperationContext
from nexus.core.read_set import AccessType, ReadSetEntry, ResourceType
from nexus.services.protocols.agent_registry import AgentInfo
from nexus.services.protocols.event_log import KernelEvent
from nexus.services.protocols.hook_engine import HookContext, HookSpec
from nexus.services.protocols.scheduler import AgentRequest

# ---------------------------------------------------------------------------
# Path strategies
# ---------------------------------------------------------------------------

# Characters allowed in path segments (no /, no null, no control chars)
_PATH_SEGMENT_CHARS = st.characters(
    whitelist_categories=("L", "N", "P", "S"),
    blacklist_characters="/\x00",
)

_PATH_SEGMENT = st.text(
    alphabet=_PATH_SEGMENT_CHARS,
    min_size=1,
    max_size=50,
)


@st.composite
def valid_path(draw: st.DrawFn, *, max_depth: int = 8) -> str:
    """Generate a valid absolute virtual path.

    Always starts with /, no path traversal, no null bytes, max 255 chars.
    """
    depth = draw(st.integers(min_value=1, max_value=max_depth))
    segments = draw(st.lists(_PATH_SEGMENT, min_size=depth, max_size=depth))
    path = "/" + "/".join(segments)
    # Truncate to 255 if needed
    return path[:255]


@st.composite
def valid_namespaced_path(
    draw: st.DrawFn,
    *,
    namespace: str | None = None,
) -> str:
    """Generate a valid path under a known namespace.

    If namespace is None, picks from the 5 default namespaces.
    """
    if namespace is None:
        namespace = draw(st.sampled_from(["workspace", "shared", "external", "system", "archives"]))
    rest = draw(valid_path(max_depth=5))
    return f"/{namespace}{rest}"


@st.composite
def valid_mount_point(draw: st.DrawFn) -> str:
    """Generate a valid mount point path (1-3 segments)."""
    depth = draw(st.integers(min_value=1, max_value=3))
    segments = draw(st.lists(_PATH_SEGMENT, min_size=depth, max_size=depth))
    return "/" + "/".join(segments)


@st.composite
def path_traversal_attempt(draw: st.DrawFn) -> str:
    """Generate a path that attempts path traversal.

    These should ALL be rejected by validate_path().
    """
    base = draw(st.sampled_from(["/workspace", "/shared", "/external"]))
    traversal = draw(
        st.sampled_from(
            [
                "/../etc/passwd",
                "/../../root",
                "/foo/../../bar",
                "/..",
                "/./../../etc",
                "/subdir/../../../escape",
            ]
        )
    )
    return base + traversal


# ---------------------------------------------------------------------------
# ReadSet strategies
# ---------------------------------------------------------------------------


@st.composite
def read_set_entry(draw: st.DrawFn) -> ReadSetEntry:
    """Generate a valid ReadSetEntry with bounded values."""
    resource_type = draw(st.sampled_from(list(ResourceType)))
    path = draw(valid_path())
    # Directories should end with /
    if resource_type == ResourceType.DIRECTORY:
        path = path.rstrip("/") + "/"
    revision = draw(st.integers(min_value=0, max_value=1_000_000))
    access_type = draw(st.sampled_from(list(AccessType)))
    return ReadSetEntry(
        resource_type=resource_type,
        resource_id=path,
        revision=revision,
        access_type=access_type,
        timestamp=draw(st.floats(min_value=0, max_value=2_000_000_000)),
    )


# ---------------------------------------------------------------------------
# OperationContext strategies
# ---------------------------------------------------------------------------

_IDENTIFIER = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), blacklist_characters=" "),
    min_size=1,
    max_size=30,
)


@st.composite
def operation_context(draw: st.DrawFn) -> OperationContext:
    """Generate a fresh OperationContext (always new instance)."""
    user = draw(_IDENTIFIER)
    groups = draw(st.lists(_IDENTIFIER, max_size=5))
    zone_id = draw(st.one_of(st.none(), _IDENTIFIER))
    is_admin = draw(st.booleans())
    subject_type = draw(st.sampled_from(["user", "agent", "service", "session"]))
    return OperationContext(
        user=user,
        groups=groups,
        zone_id=zone_id,
        is_admin=is_admin,
        subject_type=subject_type,
    )


# ---------------------------------------------------------------------------
# Scheduler strategies
# ---------------------------------------------------------------------------


@st.composite
def agent_request(
    draw: st.DrawFn,
    *,
    agent_id: str | None = None,
    zone_id: str | None = None,
) -> AgentRequest:
    """Generate a valid AgentRequest with bounded priority."""
    return AgentRequest(
        agent_id=agent_id or draw(_IDENTIFIER),
        zone_id=zone_id or draw(st.one_of(st.none(), _IDENTIFIER)),
        priority=draw(st.integers(min_value=0, max_value=100)),
        submitted_at=draw(st.text(min_size=0, max_size=30)),
        payload=draw(st.fixed_dictionaries({})),
    )


# ---------------------------------------------------------------------------
# AgentInfo / Generation Counter strategies
# ---------------------------------------------------------------------------


@st.composite
def agent_info(
    draw: st.DrawFn,
    *,
    agent_id: str | None = None,
) -> AgentInfo:
    """Generate an AgentInfo with a valid generation counter."""
    return AgentInfo(
        agent_id=agent_id or draw(_IDENTIFIER),
        owner_id=draw(_IDENTIFIER),
        zone_id=draw(st.one_of(st.none(), _IDENTIFIER)),
        name=draw(st.one_of(st.none(), _IDENTIFIER)),
        state=draw(st.sampled_from(["CONNECTED", "DISCONNECTED", "IDLE", "BUSY"])),
        generation=draw(st.integers(min_value=0, max_value=1_000_000)),
    )


# ---------------------------------------------------------------------------
# EventLog strategies
# ---------------------------------------------------------------------------


@st.composite
def kernel_event(draw: st.DrawFn) -> KernelEvent:
    """Generate a valid KernelEvent."""
    return KernelEvent(
        type=draw(
            st.sampled_from(["file_write", "file_read", "agent_connected", "agent_disconnected"])
        ),
        source=draw(st.sampled_from(["vfs_router", "scheduler", "hook_engine", "agent_registry"])),
        zone_id=draw(st.one_of(st.none(), _IDENTIFIER)),
        timestamp=draw(st.text(min_size=10, max_size=30)),
        event_id=draw(_IDENTIFIER),
        payload=draw(st.fixed_dictionaries({})),
    )


# ---------------------------------------------------------------------------
# HookEngine strategies
# ---------------------------------------------------------------------------

_HOOK_PHASES = [
    "pre_read",
    "post_read",
    "pre_write",
    "post_write",
    "pre_delete",
    "post_delete",
    "pre_mkdir",
    "post_mkdir",
]


@st.composite
def hook_spec(draw: st.DrawFn) -> HookSpec:
    """Generate a valid HookSpec."""
    return HookSpec(
        phase=draw(st.sampled_from(_HOOK_PHASES)),
        handler_name=draw(_IDENTIFIER),
        priority=draw(st.integers(min_value=-10, max_value=10)),
    )


@st.composite
def hook_context(draw: st.DrawFn) -> HookContext:
    """Generate a valid HookContext."""
    return HookContext(
        phase=draw(st.sampled_from(_HOOK_PHASES)),
        path=draw(st.one_of(st.none(), valid_path())),
        zone_id=draw(st.one_of(st.none(), _IDENTIFIER)),
        agent_id=draw(st.one_of(st.none(), _IDENTIFIER)),
        payload=draw(st.fixed_dictionaries({})),
    )
