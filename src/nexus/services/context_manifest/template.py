"""Template variable resolution for context manifest sources (Issue #1341).

Provides a simple, secure template engine that replaces ``{{variable}}``
placeholders with values from an explicit whitelist. No code execution,
no Jinja2, no injection surface.

Security model:
    - Only variables in ``ALLOWED_VARIABLES`` can be referenced.
    - Unknown variables raise ``ValueError`` immediately.
    - Missing values for allowed variables also raise ``ValueError``.
    - Single-pass replacement prevents double-substitution attacks.

References:
    - Issue #1341: Context manifest with deterministic pre-execution
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Whitelist of allowed template variables
# ---------------------------------------------------------------------------

ALLOWED_VARIABLES: frozenset[str] = frozenset(
    {
        "task.description",
        "task.id",
        "workspace.root",
        "workspace.id",
        "agent.id",
        "agent.zone_id",
        "agent.owner_id",
    }
)

# Regex to find all {{variable}} references in a template string.
_TEMPLATE_PATTERN = re.compile(r"\{\{([^}]+)\}\}")


def resolve_template(template: str, variables: dict[str, str]) -> str:
    """Replace ``{{var}}`` placeholders with values from *variables*.

    Single-pass replacement: substituted values are NOT re-scanned for
    ``{{...}}`` patterns, preventing double-substitution attacks.

    Args:
        template: The template string containing ``{{variable}}`` placeholders.
        variables: Mapping of variable names to their string values. Only
            variables listed in ``ALLOWED_VARIABLES`` are accepted.

    Returns:
        The template with all placeholders replaced by their values.

    Raises:
        ValueError: If the template references a variable not in
            ``ALLOWED_VARIABLES``, or if an allowed variable is referenced
            but not present in *variables*.

    Examples:
        >>> resolve_template("hello {{task.id}}", {"task.id": "t1"})
        'hello t1'
        >>> resolve_template("no vars here", {})
        'no vars here'
    """
    if not template:
        return template

    # Find all variable references in the template
    referenced = _TEMPLATE_PATTERN.findall(template)

    # Validate: all referenced variables must be in the whitelist
    for var_name in referenced:
        if var_name not in ALLOWED_VARIABLES:
            msg = (
                f"Template variable '{var_name}' is not allowed. "
                f"Allowed variables: {sorted(ALLOWED_VARIABLES)}"
            )
            raise ValueError(msg)

    # Validate: all referenced variables must have values provided
    for var_name in referenced:
        if var_name not in variables:
            msg = (
                f"Template variable '{var_name}' is referenced but not provided in variables dict."
            )
            raise ValueError(msg)

    # Single-pass replacement using re.sub with a callback.
    # This ensures substituted values are not re-scanned.
    def _replace(match: re.Match[str]) -> str:
        return variables[match.group(1)]

    return _TEMPLATE_PATTERN.sub(_replace, template)
