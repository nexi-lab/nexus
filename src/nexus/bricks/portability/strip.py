"""Two-layer credential stripper for archives (#3793).

Layer 1 (this file's `SchemaStripper`): nulls known sensitive columns by
table+field name and replaces them with `${PLACEHOLDER_NAME}` strings.
Records each replacement so the manifest can list what the operator must
re-inject on restore.

Layer 2 (`RegexStripper`, separate task): scans free-text fields for known
secret patterns as a backstop.
"""

from __future__ import annotations

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


__all__ = ["SchemaStripper", "StripResult"]
