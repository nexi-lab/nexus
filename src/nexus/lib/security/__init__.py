"""Security utilities for the Nexus platform (Issues #1596, #1756, #3792).

Tier-neutral security package — usable from any Nexus layer (core, services,
server) without creating cross-tier dependency violations.
"""

from nexus.lib.security.output_validator import validate_llm_output
from nexus.lib.security.policy import InjectionAction, InjectionPolicyConfig
from nexus.lib.security.prompt_sanitizer import (
    detect_injection_patterns,
    enforce_injection_policy,
    sanitize_for_prompt,
    wrap_untrusted_data,
)
from nexus.lib.security.url_validator import (
    SSRFBlocked,
    ValidatedURL,
    validate_outbound_url,
)

__all__ = [
    "InjectionAction",
    "InjectionPolicyConfig",
    "SSRFBlocked",
    "ValidatedURL",
    "detect_injection_patterns",
    "enforce_injection_policy",
    "sanitize_for_prompt",
    "validate_llm_output",
    "validate_outbound_url",
    "wrap_untrusted_data",
]
