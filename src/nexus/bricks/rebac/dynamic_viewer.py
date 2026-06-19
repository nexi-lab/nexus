"""Pure helpers for ReBAC dynamic-viewer CSV filtering."""

from typing import Any


def apply_hidden_column_filter(
    content: str,
    columns_to_hide: list[str],
    delimiter: str = ",",
) -> str:
    """Remove hidden CSV columns by header name."""
    if not columns_to_hide or not content:
        return content
    lines = content.split("\n")
    if not lines:
        return content
    header = lines[0].split(delimiter)
    hide_indices = {i for i, col in enumerate(header) if col.strip() in columns_to_hide}
    if not hide_indices:
        return content

    filtered_lines: list[str] = []
    for line in lines:
        if not line.strip():
            filtered_lines.append(line)
            continue
        cols = line.split(delimiter)
        filtered = [c for i, c in enumerate(cols) if i not in hide_indices]
        filtered_lines.append(delimiter.join(filtered))
    return "\n".join(filtered_lines)


def apply_column_config_fallback(
    content: str,
    column_config: dict[str, Any],
    delimiter: str = ",",
) -> str:
    """Apply hidden/visible column config without pandas aggregations."""
    if not content:
        return content

    aggregations = column_config.get("aggregations", {})
    hidden_columns = set(column_config.get("hidden_columns", []))
    visible_columns = column_config.get("visible_columns", [])
    aggregation_columns = set(aggregations)
    if not hidden_columns and not visible_columns and not aggregation_columns:
        return content

    lines = content.split("\n")
    if not lines:
        return content
    header = [col.strip() for col in lines[0].split(delimiter)]
    if visible_columns:
        visible_set = set(visible_columns)
        keep_indices = [
            i for i, col in enumerate(header) if col in visible_set and col not in hidden_columns
        ]
    else:
        keep_indices = [
            i
            for i, col in enumerate(header)
            if col not in hidden_columns and col not in aggregation_columns
        ]
    if not keep_indices:
        return ""

    filtered_lines: list[str] = []
    for line in lines:
        if not line.strip():
            filtered_lines.append(line)
            continue
        cols = line.split(delimiter)
        filtered_lines.append(delimiter.join(cols[i] for i in keep_indices if i < len(cols)))
    return "\n".join(filtered_lines)
