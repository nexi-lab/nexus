"""HTTP client for the Rust Search and document indexing APIs."""

from __future__ import annotations

from typing import Any

from nexus.cli.clients.base import BaseServiceClient


class SearchClient(BaseServiceClient):
    def __init__(
        self,
        url: str,
        api_key: str | None = None,
        *,
        zone_id: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(url, api_key, timeout=timeout)
        self._zone_id = zone_id

    def query(
        self,
        q: str,
        *,
        path_filter: str = "/",
        limit: int = 10,
        query_type: str = "semantic",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v2/search/query",
            json_body={
                "q": q,
                "path_filter": path_filter,
                "limit": limit,
                "query_type": query_type,
                "zone_id": self._zone_id or "",
            },
        )

    def index(self, root_path: str, *, recursive: bool = True) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v2/documents/index",
            json_body={
                "root_path": root_path,
                "recursive": recursive,
                "zone_id": self._zone_id or "",
            },
        )

    def stats(self) -> dict[str, Any]:
        return self._request("GET", "/v2/documents/stats", params={"zone_id": self._zone_id})
