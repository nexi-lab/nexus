"""Filesystem RPC handler functions.

Extracted from fastapi_server.py (#1602). Each handler accepts ``nexus_fs``
as an explicit parameter instead of reaching into the module-level global.

All sync handlers are wrapped with ``to_thread_with_timeout`` by the dispatch
layer — they MUST NOT call async code directly.
"""

from typing import TYPE_CHECKING, Any, cast

from nexus.server.path_utils import (
    unscope_internal_path,
    unscope_result,
)

if TYPE_CHECKING:
    from nexus.bricks.search.search_service import SearchService
    from nexus.core.nexus_fs import NexusFS


def _section_response_meta(section: str, results: list[Any]) -> dict[str, str]:
    """Build section filter metadata for grep responses (SSOT helper)."""
    return {
        "section_filter": section,
        "section_status": "matched" if results else "no_matches",
    }


def handle_copy(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle copy method."""
    cast(Any, nexus_fs).copy(params.src_path, params.dst_path, context=context)
    return {"copied": True}


def handle_glob(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle glob method."""
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "path") and params.path:
        kwargs["path"] = unscope_internal_path(params.path)
    # Issue #3701 (2A): forward the stateless ``files=[...]`` narrowing
    # parameter. Explicit ``is not None`` check so an intentional empty
    # list ``files=[]`` (empty-set short-circuit) is preserved.
    if hasattr(params, "files") and params.files is not None:
        kwargs["files"] = [unscope_internal_path(path) for path in params.files]

    search = nexus_fs.service("search")
    assert search is not None, "SearchService required for glob"
    matches = search.glob(params.pattern, **kwargs)
    matches = [unscope_internal_path(m) if isinstance(m, str) else m for m in matches]
    return {"matches": matches}


async def handle_grep(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle grep method."""
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "path") and params.path:
        kwargs["path"] = unscope_internal_path(params.path)
    if hasattr(params, "ignore_case") and params.ignore_case is not None:
        kwargs["ignore_case"] = params.ignore_case
    if hasattr(params, "max_results") and params.max_results is not None:
        kwargs["max_results"] = params.max_results
    if hasattr(params, "file_pattern") and params.file_pattern is not None:
        kwargs["file_pattern"] = params.file_pattern
    if hasattr(params, "search_mode") and params.search_mode is not None:
        kwargs["search_mode"] = params.search_mode
    # Pre-existing RPC drift fix (#3701 follow-up): forward the context-line
    # and invert-match params so remote SDK / MCP callers can use them.
    # Previously these were silently dropped at this allowlist boundary.
    if hasattr(params, "before_context") and params.before_context:
        kwargs["before_context"] = params.before_context
    if hasattr(params, "after_context") and params.after_context:
        kwargs["after_context"] = params.after_context
    if hasattr(params, "invert_match") and params.invert_match:
        kwargs["invert_match"] = params.invert_match
    # Issue #3701 (2A): forward the stateless ``files=[...]`` narrowing
    # parameter. Explicit ``is not None`` check so an intentional empty
    # list ``files=[]`` (empty-set short-circuit) is preserved all the
    # way through to SearchService.
    if hasattr(params, "files") and params.files is not None:
        kwargs["files"] = [unscope_internal_path(path) for path in params.files]
    if hasattr(params, "block_type") and params.block_type is not None:
        kwargs["block_type"] = params.block_type
    if hasattr(params, "section") and params.section is not None:
        kwargs["section"] = params.section

    search = nexus_fs.service("search")
    assert search is not None, "SearchService required for grep"
    results = await search.grep(params.pattern, **kwargs)
    results = [unscope_result(r) for r in results]
    response: dict[str, Any] = {"results": results}
    section = getattr(params, "section", None)
    if section is not None:
        response.update(_section_response_meta(section, results))
    return response


def handle_search(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle search method."""
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "path") and params.path:
        kwargs["path"] = params.path
    if hasattr(params, "limit") and params.limit is not None:
        kwargs["limit"] = params.limit
    if hasattr(params, "search_type") and params.search_type:
        kwargs["search_type"] = params.search_type

    results = cast(Any, nexus_fs).search(params.query, **kwargs)
    return {"results": results}


async def handle_semantic_search_index(
    nexus_fs: "NexusFS", params: Any, _context: Any
) -> dict[str, Any]:
    """Index the scoped subtree through the owning kernel's SearchService."""
    search = cast("SearchService | None", nexus_fs.service("search"))
    if search is None:
        raise ValueError("SearchService not available")
    return await search.semantic_search_index(
        path=unscope_internal_path(getattr(params, "path", "/")),
        recursive=getattr(params, "recursive", True),
        max_docs=getattr(params, "max_docs", 10_000),
        context=_context,
    )


async def handle_semantic_search(nexus_fs: "NexusFS", params: Any, _context: Any) -> dict[str, Any]:
    """Handle semantic_search through SearchService."""
    search = nexus_fs.service("search")
    if search is None:
        raise ValueError("SearchService not available")

    results = await search.semantic_search(
        query=params.query,
        path=unscope_internal_path(getattr(params, "path", "/")),
        limit=getattr(params, "limit", 10),
        search_mode=getattr(params, "search_mode", "semantic"),
        context=_context,
    )
    return {"results": results}
