from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nexus.backends.connectors.github.connector import GitHubConnector


@dataclass
class FakeResult:
    ok: bool = True
    stdout: str = "hello"


class FakeGitHub(GitHubConnector):
    def __init__(self, token: str | None = None, results: list[FakeResult] | None = None) -> None:
        self.token = token
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []
        self.reads: list[tuple[str, Any]] = []

    def _execute_cli(
        self,
        args: list[str],
        stdin: str | None = None,
        context: Any = None,
        env: dict[str, str] | None = None,
    ) -> FakeResult:
        self.calls.append({"args": list(args), "context": context, "env": env})
        if self.results:
            return self.results.pop(0)
        return FakeResult()

    def _get_user_token(self, context: Any) -> str | None:
        return self.token

    def read_content(self, content_id: str, context: Any = None) -> bytes:
        self.reads.append((content_id, context))
        return b"fallback"


class FakeGitHubByEndpoint(FakeGitHub):
    def __init__(self, responses: dict[str, FakeResult]) -> None:
        super().__init__()
        self.responses = responses

    def _execute_cli(
        self,
        args: list[str],
        stdin: str | None = None,
        context: Any = None,
        env: dict[str, str] | None = None,
    ) -> FakeResult:
        self.calls.append({"args": list(args), "context": context, "env": env})
        return self.responses.get(args[2], FakeResult(ok=False, stdout=""))


def test_github_raw_read_uses_gh_api_for_raw_paths() -> None:
    backend = FakeGitHub()
    context = object()

    assert backend.raw_read("owner/repo/main/README.md", context=context) == b"hello"

    assert backend.calls == [
        {
            "args": [
                "gh",
                "api",
                "repos/owner/repo/contents/README.md?ref=main",
                "-H",
                "Accept: application/vnd.github.raw",
            ],
            "context": context,
            "env": None,
        }
    ]
    assert backend.reads == []


def test_github_raw_read_injects_auth_env_when_token_exists() -> None:
    backend = FakeGitHub(
        token="secret-token",
        results=[
            FakeResult(ok=False, stdout=""),
            FakeResult(ok=False, stdout=""),
            FakeResult(stdout="hello"),
        ],
    )
    context = object()

    assert backend.raw_read("/owner/repo/v1.0/path/to/file.txt", context=context) == b"hello"

    assert backend.calls == [
        {
            "args": [
                "gh",
                "api",
                "repos/owner/repo/contents/file.txt?ref=v1.0/path/to",
                "-H",
                "Accept: application/vnd.github.raw",
            ],
            "context": context,
            "env": {"GH_TOKEN": "secret-token"},
        },
        {
            "args": [
                "gh",
                "api",
                "repos/owner/repo/contents/to/file.txt?ref=v1.0/path",
                "-H",
                "Accept: application/vnd.github.raw",
            ],
            "context": context,
            "env": {"GH_TOKEN": "secret-token"},
        },
        {
            "args": [
                "gh",
                "api",
                "repos/owner/repo/contents/path/to/file.txt?ref=v1.0",
                "-H",
                "Accept: application/vnd.github.raw",
            ],
            "context": context,
            "env": {"GH_TOKEN": "secret-token"},
        },
    ]


def test_github_raw_read_retries_refs_with_slashes() -> None:
    backend = FakeGitHub(results=[FakeResult(ok=False, stdout=""), FakeResult(stdout="hello")])
    context = object()

    assert backend.raw_read("owner/repo/feature/foo/src/app.py", context=context) == b"hello"

    assert [call["args"] for call in backend.calls] == [
        [
            "gh",
            "api",
            "repos/owner/repo/contents/app.py?ref=feature/foo/src",
            "-H",
            "Accept: application/vnd.github.raw",
        ],
        [
            "gh",
            "api",
            "repos/owner/repo/contents/src/app.py?ref=feature/foo",
            "-H",
            "Accept: application/vnd.github.raw",
        ],
    ]


def test_github_raw_read_prefers_longest_matching_ref() -> None:
    backend = FakeGitHubByEndpoint(
        {
            "repos/owner/repo/contents/src/app.py?ref=feature/foo": FakeResult(stdout="specific"),
            "repos/owner/repo/contents/foo/src/app.py?ref=feature": FakeResult(stdout="wrong"),
        }
    )

    assert backend.raw_read("owner/repo/feature/foo/src/app.py") == b"specific"

    assert [call["args"][2] for call in backend.calls] == [
        "repos/owner/repo/contents/app.py?ref=feature/foo/src",
        "repos/owner/repo/contents/src/app.py?ref=feature/foo",
    ]


def test_github_raw_read_falls_back_for_unsupported_paths() -> None:
    backend = FakeGitHub()
    context = object()

    assert backend.raw_read("issues/123", context=context) == b"fallback"

    assert backend.calls == []
    assert backend.reads == [("issues/123", context)]
