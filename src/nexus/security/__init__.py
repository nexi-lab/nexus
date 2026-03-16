"""Security infrastructure for the Nexus platform.

Subpackages:
    - tls/: SSH-style TOFU mTLS for gRPC zone federation (Issue #1250)

For prompt injection, LLM output validation, and SSRF protection,
use ``nexus.lib.security`` (tier-neutral utilities).
"""
