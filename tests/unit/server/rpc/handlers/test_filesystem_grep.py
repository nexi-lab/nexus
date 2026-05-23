from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.server.rpc.handlers.filesystem import handle_grep


@pytest.mark.asyncio
async def test_handle_grep_forwards_section_to_search_service() -> None:
    search = MagicMock()
    search.grep = AsyncMock(return_value=[])
    nexus_fs = MagicMock()
    nexus_fs.service.return_value = search
    params = SimpleNamespace(pattern="needle", section="## API")
    context = object()

    result = await handle_grep(nexus_fs, params, context)

    assert result == {
        "results": [],
        "section_filter": "## API",
        "section_status": "no_matches",
    }
    search.grep.assert_awaited_once_with("needle", context=context, section="## API")
