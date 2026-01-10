"""Temporal expression resolution for memory ingestion (Issue #1027).

Implements write-time disambiguation inspired by SimpleMem to convert
relative time references to absolute timestamps, making memories context-independent.

This is the Φtime stage of SimpleMem's semantic compression pipeline:
    Φextract → Φcoref → Φtime

Uses LLM-based resolution as the primary approach (most accurate),
with a regex-based heuristic fallback when no LLM is available.

References:
- SimpleMem Paper: https://arxiv.org/abs/2601.02553
- LREC-COLING 2024: Few-shot + CoT prompting best practices

Example:
    "Meeting tomorrow at 2pm" -> "Meeting on 2025-01-11 at 14:00"
    "Call back in 3 days" -> "Call back on 2025-01-13"
"""

from __future__ import annotations

import calendar
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


@dataclass
class TemporalResult:
    """Result of temporal expression resolution."""

    resolved_text: str
    original_text: str
    replacements: list[dict[str, Any]] = field(default_factory=list)
    reference_time: datetime | None = None
    method: str = "none"  # "llm", "heuristic", "none"


class TemporalResolver(ABC):
    """Abstract base class for temporal expression resolvers."""

    @abstractmethod
    def resolve(
        self,
        text: str,
        reference_time: datetime | None = None,
        context: str | None = None,
    ) -> TemporalResult:
        """Resolve temporal expressions in text.

        Args:
            text: Text to resolve temporal expressions in.
            reference_time: Reference time for resolving relative expressions.
                           Defaults to current time if not provided.
            context: Optional context for disambiguation.

        Returns:
            TemporalResult with resolved text and replacement details.
        """
        pass


class LLMTemporalResolver(TemporalResolver):
    """LLM-based temporal expression resolver (recommended approach).

    Uses a language model with few-shot + Chain of Thought prompting
    for accurate temporal resolution. This matches SimpleMem's approach.

    Examples:
        >>> resolver = LLMTemporalResolver(llm_provider)
        >>> result = resolver.resolve(
        ...     "Meeting tomorrow at 2pm",
        ...     reference_time=datetime(2025, 1, 10, 12, 0)
        ... )
        >>> print(result.resolved_text)
        "Meeting on 2025-01-11 at 14:00"
    """

    RESOLUTION_PROMPT = """You are a temporal expression resolver. Convert all relative time references to absolute dates/times.

## Instructions
1. Identify all relative temporal expressions (tomorrow, next week, in 3 days, last Monday, etc.)
2. Convert each to an absolute date/time based on the reference time
3. Preserve the rest of the text exactly as-is
4. Use format: "on YYYY-MM-DD" for dates, "at HH:MM" for times
5. If a temporal expression cannot be resolved confidently, leave it unchanged

## Examples

Reference: 2025-01-10T12:00:00
Text: "I'll call you tomorrow at 2pm"
Reasoning: "tomorrow" relative to 2025-01-10 is 2025-01-11. "2pm" is 14:00.
Resolved: "I'll call you on 2025-01-11 at 14:00"

Reference: 2025-01-10T12:00:00 (Friday)
Text: "We met last Monday and will meet again next Friday"
Reasoning: "last Monday" from Friday Jan 10 is Jan 6. "next Friday" is Jan 17.
Resolved: "We met on 2025-01-06 and will meet again on 2025-01-17"

Reference: 2025-01-10T12:00:00
Text: "The deadline is in 3 days"
Reasoning: 3 days after Jan 10 is Jan 13.
Resolved: "The deadline is on 2025-01-13"

Reference: 2025-01-10T12:00:00
Text: "I saw her yesterday morning"
Reasoning: "yesterday" from Jan 10 is Jan 9.
Resolved: "I saw her on 2025-01-09 morning"

Reference: 2025-01-10T12:00:00
Text: "The meeting is today at 3pm"
Reasoning: "today" is Jan 10, "3pm" is 15:00.
Resolved: "The meeting is on 2025-01-10 at 15:00"

## Your Task

Reference Time: {reference_time}
Text: {text}

First identify each temporal expression and reason about its absolute value.
Then provide ONLY the resolved text.

Reasoning:"""

    SIMPLE_PROMPT = """Convert relative time references to absolute dates/times.

Reference Time: {reference_time}
Text: {text}

Return ONLY the text with temporal expressions converted. Use format "on YYYY-MM-DD" for dates.

Resolved:"""

    def __init__(self, llm_provider: Any = None):
        """Initialize LLM resolver.

        Args:
            llm_provider: LLM provider instance with complete() or acomplete() method.
                         If None, will attempt to use default provider.
        """
        self.llm_provider = llm_provider

    def resolve(
        self,
        text: str,
        reference_time: datetime | None = None,
        context: str | None = None,
    ) -> TemporalResult:
        """Resolve temporal expressions using LLM.

        Args:
            text: Text to resolve.
            reference_time: Reference time (defaults to now).
            context: Optional context for disambiguation.

        Returns:
            TemporalResult with resolved text.
        """
        import asyncio

        try:
            _loop = asyncio.get_running_loop()  # noqa: F841
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    self.resolve_async(text, reference_time, context),
                )
                return future.result()
        except RuntimeError:
            return asyncio.run(self.resolve_async(text, reference_time, context))

    async def resolve_async(
        self,
        text: str,
        reference_time: datetime | None = None,
        context: str | None = None,
    ) -> TemporalResult:
        """Resolve temporal expressions using LLM (async version).

        Args:
            text: Text to resolve.
            reference_time: Reference time (defaults to now).
            context: Optional context for disambiguation.

        Returns:
            TemporalResult with resolved text.
        """
        if reference_time is None:
            reference_time = datetime.now()

        # Check if text has temporal expressions
        if not self._has_temporal_expressions(text):
            return TemporalResult(
                resolved_text=text,
                original_text=text,
                replacements=[],
                reference_time=reference_time,
                method="none",
            )

        # Get or create LLM provider
        provider = self.llm_provider
        if provider is None:
            provider = self._get_default_provider()

        if provider is None:
            # Fall back to heuristic
            fallback = HeuristicTemporalResolver()
            result = fallback.resolve(text, reference_time, context)
            result.method = "heuristic"
            return result

        # Format reference time
        ref_str = reference_time.strftime("%Y-%m-%dT%H:%M:%S")
        weekday = reference_time.strftime("%A")
        ref_display = f"{ref_str} ({weekday})"

        # Choose prompt based on context
        if context and len(context) > 30:
            prompt = self.RESOLUTION_PROMPT.format(
                reference_time=ref_display,
                text=text,
            )
        else:
            prompt = self.SIMPLE_PROMPT.format(
                reference_time=ref_display,
                text=text,
            )

        try:
            # Call LLM - handle different provider interfaces
            if hasattr(provider, "complete_async"):
                from nexus.llm import Message, MessageRole

                messages = [Message(role=MessageRole.USER, content=prompt)]
                response = await provider.complete_async(messages)
            elif hasattr(provider, "acomplete"):
                response = await provider.acomplete(prompt)
            elif hasattr(provider, "complete"):
                if hasattr(provider, "config"):
                    from nexus.llm import Message, MessageRole

                    messages = [Message(role=MessageRole.USER, content=prompt)]
                    response = provider.complete(messages)
                else:
                    response = provider.complete(prompt)
            else:
                raise ValueError("LLM provider must have complete() or complete_async() method")

            # Extract resolved text
            resolved_text = self._extract_resolved_text(response, text)

            # Detect replacements
            replacements = self._detect_replacements(text, resolved_text)

            return TemporalResult(
                resolved_text=resolved_text,
                original_text=text,
                replacements=replacements,
                reference_time=reference_time,
                method="llm",
            )

        except Exception as e:
            import logging

            logging.getLogger(__name__).warning(
                f"LLM temporal resolution failed: {e}, falling back to heuristic"
            )
            fallback = HeuristicTemporalResolver()
            result = fallback.resolve(text, reference_time, context)
            result.method = "heuristic"
            return result

    def _has_temporal_expressions(self, text: str) -> bool:
        """Check if text contains temporal expressions."""
        patterns = [
            r"\btoday\b",
            r"\btomorrow\b",
            r"\byesterday\b",
            r"\bnext\s+(week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            r"\blast\s+(week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            r"\bin\s+\d+\s+(days?|weeks?|months?|hours?|minutes?)\b",
            r"\b\d+\s+(days?|weeks?|months?|hours?|minutes?)\s+ago\b",
            r"\bthis\s+(week|month|year|weekend|morning|afternoon|evening)\b",
            r"\bthe\s+day\s+after\s+tomorrow\b",
            r"\bthe\s+day\s+before\s+yesterday\b",
        ]
        text_lower = text.lower()
        return any(re.search(p, text_lower) for p in patterns)

    def _extract_resolved_text(self, response: Any, original: str) -> str:
        """Extract resolved text from LLM response."""
        text: str
        if hasattr(response, "content"):
            text = str(response.content)
        elif hasattr(response, "text"):
            text = str(response.text)
        elif isinstance(response, str):
            text = response
        else:
            text = str(response)

        text = text.strip()

        # Extract after markers
        markers = ["Resolved:", "Resolved text:", "Output:", "Result:"]
        for marker in markers:
            if marker.lower() in text.lower():
                idx = text.lower().rfind(marker.lower())
                text = text[idx + len(marker) :].strip()
                break

        # Clean quotes
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        if text.startswith("'") and text.endswith("'"):
            text = text[1:-1]

        # Validate response length
        if len(text) < len(original) * 0.5 or len(text) > len(original) * 3:
            return original

        return text

    def _detect_replacements(self, original: str, resolved: str) -> list[dict[str, Any]]:
        """Detect temporal expressions that were replaced."""
        replacements = []

        patterns = [
            (r"\btoday\b", "today"),
            (r"\btomorrow\b", "tomorrow"),
            (r"\byesterday\b", "yesterday"),
            (r"\bnext\s+\w+\b", "next X"),
            (r"\blast\s+\w+\b", "last X"),
            (r"\bin\s+\d+\s+\w+\b", "in N units"),
            (r"\b\d+\s+\w+\s+ago\b", "N units ago"),
            (r"\bthis\s+\w+\b", "this X"),
        ]

        for pattern, expr_type in patterns:
            matches = re.findall(pattern, original, re.IGNORECASE)
            for match in matches:
                if match.lower() not in resolved.lower():
                    replacements.append(
                        {
                            "original": match,
                            "type": expr_type,
                            "resolved": True,
                        }
                    )

        return replacements

    def _get_default_provider(self) -> Any:
        """Try to get a default LLM provider."""
        import os

        try:
            from pydantic import SecretStr

            from nexus.llm import LiteLLMProvider, LLMConfig

            if os.environ.get("ANTHROPIC_API_KEY"):
                config = LLMConfig(
                    model="claude-sonnet-4-20250514",
                    api_key=SecretStr(os.environ["ANTHROPIC_API_KEY"]),
                    max_output_tokens=1024,
                )
                return LiteLLMProvider(config)
            elif os.environ.get("OPENAI_API_KEY"):
                config = LLMConfig(
                    model="gpt-4o-mini",
                    api_key=SecretStr(os.environ["OPENAI_API_KEY"]),
                    max_output_tokens=1024,
                )
                return LiteLLMProvider(config)
        except Exception:
            pass

        return None


class HeuristicTemporalResolver(TemporalResolver):
    """Regex-based temporal expression resolver (fallback only).

    Uses pattern matching for common temporal expressions when no LLM is available.
    Less accurate than LLM-based resolution but works offline.

    Note: This should only be used as a fallback. SimpleMem research shows
    removing proper temporal resolution significantly impacts retrieval accuracy.
    """

    WEEKDAYS = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }

    def resolve(
        self,
        text: str,
        reference_time: datetime | None = None,
        _context: str | None = None,
    ) -> TemporalResult:
        """Resolve temporal expressions using heuristics.

        Args:
            text: Text to resolve.
            reference_time: Reference time (defaults to now).
            _context: Ignored in heuristic resolver.

        Returns:
            TemporalResult with resolved text.
        """
        if reference_time is None:
            reference_time = datetime.now()

        resolved_text = text
        replacements = []

        # Process each pattern
        resolved_text, reps = self._resolve_today(resolved_text, reference_time)
        replacements.extend(reps)

        resolved_text, reps = self._resolve_tomorrow(resolved_text, reference_time)
        replacements.extend(reps)

        resolved_text, reps = self._resolve_yesterday(resolved_text, reference_time)
        replacements.extend(reps)

        resolved_text, reps = self._resolve_in_n_days(resolved_text, reference_time)
        replacements.extend(reps)

        resolved_text, reps = self._resolve_n_days_ago(resolved_text, reference_time)
        replacements.extend(reps)

        resolved_text, reps = self._resolve_next_weekday(resolved_text, reference_time)
        replacements.extend(reps)

        resolved_text, reps = self._resolve_last_weekday(resolved_text, reference_time)
        replacements.extend(reps)

        resolved_text, reps = self._resolve_next_week(resolved_text, reference_time)
        replacements.extend(reps)

        resolved_text, reps = self._resolve_last_week(resolved_text, reference_time)
        replacements.extend(reps)

        resolved_text, reps = self._resolve_next_month(resolved_text, reference_time)
        replacements.extend(reps)

        resolved_text, reps = self._resolve_last_month(resolved_text, reference_time)
        replacements.extend(reps)

        method = "heuristic" if replacements else "none"

        return TemporalResult(
            resolved_text=resolved_text,
            original_text=text,
            replacements=replacements,
            reference_time=reference_time,
            method=method,
        )

    def _resolve_today(self, text: str, ref: datetime) -> tuple[str, list[dict]]:
        """Replace 'today' with absolute date."""
        replacements = []
        date_str = ref.strftime("%Y-%m-%d")

        def replace(match: re.Match) -> str:
            original = match.group(0)
            replacement = f"on {date_str}"
            replacements.append(
                {
                    "original": original,
                    "resolved": replacement,
                    "type": "today",
                }
            )
            return replacement

        result = re.sub(r"\btoday\b", replace, text, flags=re.IGNORECASE)
        return result, replacements

    def _resolve_tomorrow(self, text: str, ref: datetime) -> tuple[str, list[dict]]:
        """Replace 'tomorrow' with absolute date."""
        replacements = []
        date_str = (ref + timedelta(days=1)).strftime("%Y-%m-%d")

        def replace(match: re.Match) -> str:
            original = match.group(0)
            replacement = f"on {date_str}"
            replacements.append(
                {
                    "original": original,
                    "resolved": replacement,
                    "type": "tomorrow",
                }
            )
            return replacement

        result = re.sub(r"\btomorrow\b", replace, text, flags=re.IGNORECASE)
        return result, replacements

    def _resolve_yesterday(self, text: str, ref: datetime) -> tuple[str, list[dict]]:
        """Replace 'yesterday' with absolute date."""
        replacements = []
        date_str = (ref - timedelta(days=1)).strftime("%Y-%m-%d")

        def replace(match: re.Match) -> str:
            original = match.group(0)
            replacement = f"on {date_str}"
            replacements.append(
                {
                    "original": original,
                    "resolved": replacement,
                    "type": "yesterday",
                }
            )
            return replacement

        result = re.sub(r"\byesterday\b", replace, text, flags=re.IGNORECASE)
        return result, replacements

    def _resolve_in_n_days(self, text: str, ref: datetime) -> tuple[str, list[dict]]:
        """Replace 'in N days' with absolute date."""
        replacements = []

        def replace(match: re.Match) -> str:
            original = match.group(0)
            n = int(match.group(1))
            date_str = (ref + timedelta(days=n)).strftime("%Y-%m-%d")
            replacement = f"on {date_str}"
            replacements.append(
                {
                    "original": original,
                    "resolved": replacement,
                    "type": "in_n_days",
                }
            )
            return replacement

        result = re.sub(r"\bin\s+(\d+)\s+days?\b", replace, text, flags=re.IGNORECASE)
        return result, replacements

    def _resolve_n_days_ago(self, text: str, ref: datetime) -> tuple[str, list[dict]]:
        """Replace 'N days ago' with absolute date."""
        replacements = []

        def replace(match: re.Match) -> str:
            original = match.group(0)
            n = int(match.group(1))
            date_str = (ref - timedelta(days=n)).strftime("%Y-%m-%d")
            replacement = f"on {date_str}"
            replacements.append(
                {
                    "original": original,
                    "resolved": replacement,
                    "type": "n_days_ago",
                }
            )
            return replacement

        result = re.sub(r"\b(\d+)\s+days?\s+ago\b", replace, text, flags=re.IGNORECASE)
        return result, replacements

    def _resolve_next_weekday(self, text: str, ref: datetime) -> tuple[str, list[dict]]:
        """Replace 'next Monday' etc with absolute date."""
        replacements = []

        def replace(match: re.Match) -> str:
            original: str = match.group(0)
            weekday_name = match.group(1).lower()
            target_weekday = self.WEEKDAYS.get(weekday_name)
            if target_weekday is None:
                return original

            current_weekday = ref.weekday()
            days_ahead = target_weekday - current_weekday
            if days_ahead <= 0:
                days_ahead += 7

            date_str = (ref + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
            replacement = f"on {date_str}"
            replacements.append(
                {
                    "original": original,
                    "resolved": replacement,
                    "type": "next_weekday",
                }
            )
            return replacement

        pattern = r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
        result = re.sub(pattern, replace, text, flags=re.IGNORECASE)
        return result, replacements

    def _resolve_last_weekday(self, text: str, ref: datetime) -> tuple[str, list[dict]]:
        """Replace 'last Monday' etc with absolute date."""
        replacements = []

        def replace(match: re.Match) -> str:
            original: str = match.group(0)
            weekday_name = match.group(1).lower()
            target_weekday = self.WEEKDAYS.get(weekday_name)
            if target_weekday is None:
                return original

            current_weekday = ref.weekday()
            days_back = current_weekday - target_weekday
            if days_back <= 0:
                days_back += 7

            date_str = (ref - timedelta(days=days_back)).strftime("%Y-%m-%d")
            replacement = f"on {date_str}"
            replacements.append(
                {
                    "original": original,
                    "resolved": replacement,
                    "type": "last_weekday",
                }
            )
            return replacement

        pattern = r"\blast\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
        result = re.sub(pattern, replace, text, flags=re.IGNORECASE)
        return result, replacements

    def _resolve_next_week(self, text: str, ref: datetime) -> tuple[str, list[dict]]:
        """Replace 'next week' with date range."""
        replacements = []

        def replace(match: re.Match) -> str:
            original = match.group(0)
            # Start of next week (Monday)
            days_until_monday = (7 - ref.weekday()) % 7
            if days_until_monday == 0:
                days_until_monday = 7
            next_monday = ref + timedelta(days=days_until_monday)
            date_str = next_monday.strftime("%Y-%m-%d")
            replacement = f"the week of {date_str}"
            replacements.append(
                {
                    "original": original,
                    "resolved": replacement,
                    "type": "next_week",
                }
            )
            return replacement

        result = re.sub(r"\bnext\s+week\b", replace, text, flags=re.IGNORECASE)
        return result, replacements

    def _resolve_last_week(self, text: str, ref: datetime) -> tuple[str, list[dict]]:
        """Replace 'last week' with date range."""
        replacements = []

        def replace(match: re.Match) -> str:
            original = match.group(0)
            # Start of last week (Monday)
            days_since_monday = ref.weekday()
            last_monday = ref - timedelta(days=days_since_monday + 7)
            date_str = last_monday.strftime("%Y-%m-%d")
            replacement = f"the week of {date_str}"
            replacements.append(
                {
                    "original": original,
                    "resolved": replacement,
                    "type": "last_week",
                }
            )
            return replacement

        result = re.sub(r"\blast\s+week\b", replace, text, flags=re.IGNORECASE)
        return result, replacements

    def _resolve_next_month(self, text: str, ref: datetime) -> tuple[str, list[dict]]:
        """Replace 'next month' with month name."""
        replacements = []

        def replace(match: re.Match) -> str:
            original = match.group(0)
            if ref.month == 12:
                next_month = 1
                next_year = ref.year + 1
            else:
                next_month = ref.month + 1
                next_year = ref.year
            month_name = calendar.month_name[next_month]
            replacement = f"{month_name} {next_year}"
            replacements.append(
                {
                    "original": original,
                    "resolved": replacement,
                    "type": "next_month",
                }
            )
            return replacement

        result = re.sub(r"\bnext\s+month\b", replace, text, flags=re.IGNORECASE)
        return result, replacements

    def _resolve_last_month(self, text: str, ref: datetime) -> tuple[str, list[dict]]:
        """Replace 'last month' with month name."""
        replacements = []

        def replace(match: re.Match) -> str:
            original = match.group(0)
            if ref.month == 1:
                last_month = 12
                last_year = ref.year - 1
            else:
                last_month = ref.month - 1
                last_year = ref.year
            month_name = calendar.month_name[last_month]
            replacement = f"{month_name} {last_year}"
            replacements.append(
                {
                    "original": original,
                    "resolved": replacement,
                    "type": "last_month",
                }
            )
            return replacement

        result = re.sub(r"\blast\s+month\b", replace, text, flags=re.IGNORECASE)
        return result, replacements


# Convenience functions

_default_resolver: TemporalResolver | None = None


def get_temporal_resolver(llm_provider: Any = None) -> TemporalResolver:
    """Get a temporal expression resolver.

    Args:
        llm_provider: Optional LLM provider. If provided, returns LLM resolver.
                     If None, returns cached default resolver.

    Returns:
        TemporalResolver instance.
    """
    if llm_provider is not None:
        return LLMTemporalResolver(llm_provider)

    global _default_resolver
    if _default_resolver is None:
        _default_resolver = LLMTemporalResolver()
    return _default_resolver


def resolve_temporal(
    text: str,
    reference_time: datetime | str | None = None,
    context: str | None = None,
    llm_provider: Any = None,
) -> str:
    """Resolve temporal expressions in text.

    Args:
        text: Text with temporal expressions to resolve.
        reference_time: Reference time for resolution (defaults to now).
                       Can be datetime or ISO-8601 string.
        context: Optional context for disambiguation.
        llm_provider: Optional LLM provider.

    Returns:
        Text with temporal expressions replaced by absolute dates.
    """
    # Parse reference time if string
    if isinstance(reference_time, str):
        reference_time = datetime.fromisoformat(reference_time.replace("Z", "+00:00"))

    resolver = get_temporal_resolver(llm_provider)
    result = resolver.resolve(text, reference_time, context)
    return result.resolved_text


def extract_temporal_metadata(
    text: str,
    reference_time: datetime | str | None = None,
) -> dict[str, Any]:
    """Extract temporal metadata from text for database storage.

    Issue #1028: Extracts temporal references for date-based queries.
    Unlike resolve_temporal(), this function returns metadata about
    temporal references without modifying the text.

    Args:
        text: Text to extract temporal references from.
        reference_time: Reference time for resolving relative expressions.
                       Can be datetime or ISO-8601 string.
                       Defaults to current time if not provided.

    Returns:
        Dictionary with:
        - temporal_refs: List of extracted temporal references with original
                        text and resolved dates
        - earliest_date: Earliest datetime mentioned in content (or None)
        - latest_date: Latest datetime mentioned in content (or None)

    Example:
        >>> from datetime import datetime
        >>> result = extract_temporal_metadata(
        ...     "Meeting tomorrow, follow-up in 3 days",
        ...     reference_time=datetime(2025, 1, 10, 12, 0)
        ... )
        >>> result["temporal_refs"]
        [{"original": "tomorrow", "resolved": "2025-01-11", "type": "tomorrow"},
         {"original": "in 3 days", "resolved": "2025-01-13", "type": "in_x_days"}]
        >>> result["earliest_date"]
        datetime(2025, 1, 11, 0, 0)
        >>> result["latest_date"]
        datetime(2025, 1, 13, 0, 0)
    """
    # Parse reference time if string
    if isinstance(reference_time, str):
        reference_time = datetime.fromisoformat(reference_time.replace("Z", "+00:00"))

    # Use heuristic resolver for extraction (no LLM needed)
    resolver = HeuristicTemporalResolver()
    result = resolver.resolve(text, reference_time)

    # Extract dates from replacements
    dates: list[datetime] = []
    for replacement in result.replacements:
        resolved = replacement.get("resolved", "")
        # Try to parse date from resolved string
        parsed_date = _parse_date_from_resolved(resolved)
        if parsed_date:
            dates.append(parsed_date)

    # Calculate earliest and latest dates
    earliest_date = min(dates) if dates else None
    latest_date = max(dates) if dates else None

    return {
        "temporal_refs": result.replacements,
        "earliest_date": earliest_date,
        "latest_date": latest_date,
    }


def _parse_date_from_resolved(
    resolved: str, _reference_time: datetime | None = None
) -> datetime | None:
    """Parse a datetime from a resolved temporal string.

    Handles various formats like:
    - "2025-01-11" (ISO date)
    - "on 2025-01-11" (with prefix)
    - "January 2025" (month year)
    - "week of 2025-01-13" (week prefix)

    Args:
        resolved: Resolved temporal string.
        reference_time: Reference time for year inference.

    Returns:
        Parsed datetime or None if parsing fails.
    """
    if not resolved:
        return None

    # Try ISO date format (YYYY-MM-DD)
    iso_match = re.search(r"\d{4}-\d{2}-\d{2}", resolved)
    if iso_match:
        try:
            return datetime.strptime(iso_match.group(), "%Y-%m-%d")
        except ValueError:
            pass

    # Try month year format (e.g., "January 2025")
    month_year_match = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
        resolved,
    )
    if month_year_match:
        try:
            month_name = month_year_match.group(1)
            year = int(month_year_match.group(2))
            month_num = list(calendar.month_name).index(month_name)
            return datetime(year, month_num, 1)
        except (ValueError, IndexError):
            pass

    return None
