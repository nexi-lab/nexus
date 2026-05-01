"""Two-layer credential stripper for archives (#3793).

Layer 1 (this file's `SchemaStripper`): nulls known sensitive columns by
table+field name and replaces them with `${PLACEHOLDER_NAME}` strings.
Records each replacement so the manifest can list what the operator must
re-inject on restore.

Layer 2 (`RegexStripper`, separate task): scans free-text fields for known
secret patterns as a backstop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from nexus.bricks.portability.models import PlaceholderRef


@dataclass
class StripResult:
    rows: list[dict[str, Any]]
    placeholders: list[PlaceholderRef] = field(default_factory=list)


# (table, sensitive_field, name_field, placeholder_template, dotted_field_template)
_SCHEMA_RULES: list[tuple[str, str, str, str, str]] = [
    ("providers", "api_key", "name", "PROVIDER_KEY_{name}", "providers.{name}.api_key"),
    ("federations", "auth_token", "name", "HUB_TOKEN_{name}", "federations.{name}.auth_token"),
    ("webhooks", "secret", "name", "WEBHOOK_SECRET_{name}", "webhooks.{name}.secret"),
]

_DENY_LIST_SETTING_KEYS = frozenset(
    {"hub_auth_token", "anthropic_api_key", "openai_api_key", "google_api_key"}
)


class SchemaStripper:
    """Strip credentials from known sensitive columns by table + field."""

    def __init__(self, workspace_root: str | None = None) -> None:
        self.workspace_root = workspace_root

    def strip_table(self, table: str, rows: list[dict[str, Any]]) -> StripResult:
        out_rows: list[dict[str, Any]] = []
        placeholders: list[PlaceholderRef] = []
        rules = [r for r in _SCHEMA_RULES if r[0] == table]
        for row in rows:
            new_row = dict(row)
            for _t, sensitive, name_field, ph_tpl, field_tpl in rules:
                if sensitive in new_row and new_row[sensitive] is not None:
                    name = str(new_row.get(name_field, "unknown"))
                    placeholder_name = ph_tpl.format(name=name)
                    new_row[sensitive] = f"${{{placeholder_name}}}"
                    placeholders.append(
                        PlaceholderRef(name=placeholder_name, field=field_tpl.format(name=name))
                    )
            if table == "settings" and new_row.get("key") in _DENY_LIST_SETTING_KEYS:
                key = new_row["key"]
                placeholder_name = f"SETTING_{key}"
                if new_row.get("value") is not None:
                    new_row["value"] = f"${{{placeholder_name}}}"
                    placeholders.append(
                        PlaceholderRef(name=placeholder_name, field=f"settings.{key}.value")
                    )
            if self.workspace_root and table == "documents" and "path" in new_row:
                p = new_row["path"]
                if isinstance(p, str) and p.startswith(self.workspace_root):
                    new_row["path"] = "${WORKSPACE_ROOT}" + p[len(self.workspace_root) :]
            out_rows.append(new_row)
        return StripResult(rows=out_rows, placeholders=placeholders)


@dataclass
class RegexMatch:
    pattern_name: str
    location: str
    snippet: str


@dataclass
class RegexStripResult:
    text: str
    matches: list[RegexMatch]


DEFAULT_REDACT_PATTERNS: list[dict[str, str]] = [
    {"name": "anthropic", "pattern": r"sk-ant-[A-Za-z0-9_-]{20,}"},
    {"name": "openai", "pattern": r"sk-[A-Za-z0-9]{20,}"},
    {"name": "github_pat", "pattern": r"ghp_[A-Za-z0-9]{36}"},
    {"name": "github_oauth", "pattern": r"gho_[A-Za-z0-9]{36}"},
    {"name": "gitlab_pat", "pattern": r"glpat-[A-Za-z0-9_-]{20}"},
    {"name": "slack_bot", "pattern": r"xoxb-[0-9]+-[0-9]+-[A-Za-z0-9]+"},
    {"name": "aws_access_key", "pattern": r"AKIA[0-9A-Z]{16}"},
    {"name": "google_api_key", "pattern": r"AIza[0-9A-Za-z_-]{35}"},
]


class RegexStripper:
    """Backstop credential scanner over free-text fields."""

    def __init__(self, patterns: list[dict[str, str]]) -> None:
        self._compiled: list[tuple[str, re.Pattern[str]]] = []
        for p in patterns:
            try:
                self._compiled.append((p["name"], re.compile(p["pattern"])))
            except re.error as e:
                raise ValueError(f"Invalid regex {p['name']!r}: {e}") from e

    def scan(self, text: str, *, location: str) -> RegexStripResult:
        if not text:
            return RegexStripResult(text=text, matches=[])
        matches: list[RegexMatch] = []
        out = text
        for name, rx in self._compiled:
            for m in list(rx.finditer(out)):
                matches.append(
                    RegexMatch(
                        pattern_name=name,
                        location=location,
                        snippet=m.group(0)[:8] + "…",
                    )
                )
            out = rx.sub("***REDACTED***", out)
        return RegexStripResult(text=out, matches=matches)


__all__ = [
    "SchemaStripper",
    "StripResult",
    "RegexStripper",
    "RegexStripResult",
    "RegexMatch",
    "DEFAULT_REDACT_PATTERNS",
]
