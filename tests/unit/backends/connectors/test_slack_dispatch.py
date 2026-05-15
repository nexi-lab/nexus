from __future__ import annotations

from typing import Any, cast

from nexus.backends.connectors.slack.connector import PathSlackBackend
from nexus.backends.connectors.slack.transport import SlackTransport
from nexus.contracts.types import OperationContext


class FakeClient:
    def search_messages(self, query: str, count: int = 20) -> dict[str, object]:
        assert query == "error"
        assert count == 2
        return {
            "ok": True,
            "messages": {
                "matches": [
                    {
                        "channel": {"name": "general"},
                        "text": "first error",
                        "ts": "1.000",
                    },
                    {
                        "channel": {"name": "random"},
                        "text": "second error",
                        "ts": "2.000",
                    },
                ]
            },
        }


class FakeSlackTransport(SlackTransport):
    def __init__(self) -> None:
        self._context = None
        self._max_messages_per_channel = 100

    def _get_slack_client(self) -> FakeClient:
        return FakeClient()


def test_slack_transport_search_messages_maps_to_grep_shape() -> None:
    transport = FakeSlackTransport()
    matches = transport.search_messages("error", max_results=2, ignore_case=False)
    assert matches == [
        {
            "file": "/slack/channels/general.yaml",
            "line": 1,
            "content": "first error",
            "match": "error",
        },
        {
            "file": "/slack/channels/random.yaml",
            "line": 1,
            "content": "second error",
            "match": "error",
        },
    ]


def test_slack_transport_search_messages_filters_path_scope_and_mount() -> None:
    transport = FakeSlackTransport()
    matches = transport.search_messages(
        "error",
        max_results=2,
        ignore_case=False,
        backend_path="channels/general.yaml",
        mount_path="/chat",
    )
    assert matches == [
        {
            "file": "/chat/channels/general.yaml",
            "line": 1,
            "content": "first error",
            "match": "error",
        }
    ]


class FakePagedClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, int | str]] = []

    def search_messages(self, query: str, count: int = 20, page: int = 1) -> dict[str, object]:
        self.calls.append({"query": query, "count": count, "page": page})
        if page == 1:
            return {
                "ok": True,
                "messages": {
                    "matches": [
                        {
                            "channel": {"name": "random"},
                            "text": "outside scope error",
                            "ts": "1.000",
                        }
                    ],
                    "pagination": {"page": 1, "page_count": 2},
                },
            }
        return {
            "ok": True,
            "messages": {
                "matches": [
                    {
                        "channel": {"name": "general"},
                        "text": "scoped error",
                        "ts": "2.000",
                    }
                ],
                "pagination": {"page": 2, "page_count": 2},
            },
        }


class FakePagedSlackTransport(SlackTransport):
    def __init__(self) -> None:
        self.client = FakePagedClient()
        self._context = None
        self._max_messages_per_channel = 100

    def _get_slack_client(self) -> FakePagedClient:
        return self.client


def test_slack_transport_search_messages_pages_until_scoped_match() -> None:
    transport = FakePagedSlackTransport()

    matches = transport.search_messages(
        "error",
        max_results=1,
        ignore_case=False,
        backend_path="channels/general.yaml",
        mount_path="/chat",
    )

    assert matches == [
        {
            "file": "/chat/channels/general.yaml",
            "line": 1,
            "content": "scoped error",
            "match": "error",
        }
    ]
    assert transport.client.calls == [
        {"query": "error", "count": 1, "page": 1},
        {"query": "error", "count": 1, "page": 2},
    ]


class FakeConnector(PathSlackBackend):
    def __init__(self) -> None:
        self.bound_context: OperationContext | None = None
        self._transport = FakeTransport()

    def _bind_transport(self, context: OperationContext | None) -> None:
        self.bound_context = context


class FakeTransport(SlackTransport):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search_messages(
        self,
        pattern: str,
        *,
        max_results: int = 100,
        ignore_case: bool = False,
        backend_path: str = "",
        mount_path: str = "/slack",
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "pattern": pattern,
                "max_results": max_results,
                "ignore_case": ignore_case,
                "backend_path": backend_path,
                "mount_path": mount_path,
            }
        )
        return [{"file": "/slack/channels/general.yaml"}]


def test_slack_connector_grep_messages_binds_context_and_delegates() -> None:
    backend = FakeConnector()
    context = OperationContext(user_id="user", groups=[])
    matches = backend.grep_messages(
        "error",
        context=context,
        max_results=2,
        ignore_case=True,
        backend_path="channels/general.yaml",
        mount_path="/chat",
    )
    assert matches == [{"file": "/slack/channels/general.yaml"}]
    assert backend.bound_context == context
    assert cast(FakeTransport, backend._transport).calls == [
        {
            "pattern": "error",
            "max_results": 2,
            "ignore_case": True,
            "backend_path": "channels/general.yaml",
            "mount_path": "/chat",
        }
    ]
