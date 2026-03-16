"""ContentParserEngine — on-demand content parsing with metadata caching.

Extracted from NexusFS kernel (Issue #1383). The kernel should not
parse content; that is a brick-layer concern.

DI dependencies:
  - metadata: MetastoreABC (for parsed_text cache)
  - provider_registry: ProviderRegistry (for selecting parse provider)
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ContentParserEngine:
    """On-demand content parsing with metadata cache.

    Checks for cached ``parsed_text`` in metastore first, then falls back
    to provider-based parsing.  Results are cached for subsequent reads.
    """

    def __init__(
        self,
        metadata: Any,
        provider_registry: Any | None = None,
    ) -> None:
        self._metadata = metadata
        self._provider_registry = provider_registry

    async def get_parsed_content_async(
        self, path: str, content: bytes
    ) -> tuple[bytes, dict[str, Any]]:
        """Get parsed content for a file (async).

        First checks for cached parsed_text in metadata, then parses
        on-demand if a provider is available.  Falls back to raw content
        if parsing fails.

        Returns:
            (parsed_content_bytes, parse_info_dict)
        """
        parse_info: dict[str, Any] = {"parsed": False, "provider": None, "cached": False}

        try:
            cached_text = self._metadata.get_file_metadata(path, "parsed_text")
            if cached_text:
                parse_info["parsed"] = True
                parse_info["cached"] = True
                parse_info["provider"] = self._metadata.get_file_metadata(path, "parser_name")
                logger.debug(f"Using cached parsed_text for {path}")
                return (
                    cached_text.encode("utf-8") if isinstance(cached_text, str) else cached_text,
                    parse_info,
                )

            if self._provider_registry is None:
                logger.debug(f"No provider registry available for parsing {path}")
                return content, parse_info

            provider = self._provider_registry.get_provider(path)
            if not provider:
                logger.debug(f"No parse provider available for {path}")
                return content, parse_info

            try:
                result = await provider.parse(content, path)

                if result and result.text:
                    parse_info["parsed"] = True
                    parse_info["provider"] = provider.name
                    parsed_content = result.text.encode("utf-8")

                    try:
                        from datetime import UTC, datetime

                        self._metadata.set_file_metadata(path, "parsed_text", result.text)
                        self._metadata.set_file_metadata(
                            path, "parsed_at", datetime.now(UTC).isoformat()
                        )
                        self._metadata.set_file_metadata(path, "parser_name", provider.name)
                    except Exception as cache_err:
                        logger.warning(f"Failed to cache parsed content for {path}: {cache_err}")

                    return parsed_content, parse_info

            except Exception as parse_err:
                logger.warning(f"Failed to parse {path} with {provider.name}: {parse_err}")
                return content, parse_info

        except Exception as e:
            logger.warning(f"Error getting parsed content for {path}: {e}")

        return content, parse_info

    def get_parsed_content(self, path: str, content: bytes) -> tuple[bytes, dict[str, Any]]:
        """Get parsed content for a file (sync wrapper)."""
        import asyncio

        try:
            asyncio.get_running_loop()
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, self.get_parsed_content_async(path, content))
                return future.result()
        except RuntimeError:
            from nexus.lib.sync_bridge import run_sync

            return run_sync(self.get_parsed_content_async(path, content))
