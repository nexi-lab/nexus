"""LLM domain client (async-only).

Decision #4: Move LLM methods to async client only (remove from sync).

Issue #1603: Decompose remote/client.py into domain clients.
"""

from __future__ import annotations

from typing import Any


class AsyncLLMClient:
    """Async LLM client — delegates to LLMService.

    Unlike other domain clients, this doesn't use _call_rpc.
    It delegates to the LLMService instance which calls LLM APIs directly.
    """

    def __init__(self, get_llm_service: Any) -> None:
        self._get_llm_service = get_llm_service

    async def read(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
        api_key: str | None = None,
        use_search: bool = True,
        search_mode: str = "semantic",
        provider: Any = None,
    ) -> str:
        result: str = await self._get_llm_service().llm_read(
            path=path,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            api_key=api_key,
            use_search=use_search,
            search_mode=search_mode,
            provider=provider,
        )
        return result

    async def read_detailed(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
        api_key: str | None = None,
        use_search: bool = True,
        search_mode: str = "semantic",
        provider: Any = None,
    ) -> Any:
        return await self._get_llm_service().llm_read_detailed(
            path=path,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            api_key=api_key,
            use_search=use_search,
            search_mode=search_mode,
            provider=provider,
        )

    async def read_stream(
        self,
        path: str,
        prompt: str,
        model: str = "claude-sonnet-4",
        max_tokens: int = 1000,
        api_key: str | None = None,
        use_search: bool = True,
        search_mode: str = "semantic",
        provider: Any = None,
    ) -> Any:
        return self._get_llm_service().llm_read_stream(
            path=path,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            api_key=api_key,
            use_search=use_search,
            search_mode=search_mode,
            provider=provider,
        )

    def create_reader(
        self,
        provider: Any = None,
        model: str | None = None,
        api_key: str | None = None,
        system_prompt: str | None = None,
        max_context_tokens: int = 3000,
    ) -> Any:
        return self._get_llm_service().create_llm_reader(
            provider=provider,
            model=model,
            api_key=api_key,
            system_prompt=system_prompt,
            max_context_tokens=max_context_tokens,
        )
