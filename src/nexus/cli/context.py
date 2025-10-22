"""CLI context management - Global state and configuration."""

from __future__ import annotations

import click

# Global context for passing state between commands
pass_context = click.make_pass_decorator(dict, ensure=True)
