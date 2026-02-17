"""Backward-compat shim — canonical module is nexus.auth.slack_oauth."""

from nexus.auth.slack_oauth import SlackOAuthProvider  # noqa: F401

__all__ = ["SlackOAuthProvider"]
