"""Built-in OAuth provider subclasses (vendor quirks on UniversalOAuthProvider)."""

from nexus.lib.oauth.providers.google import GoogleOAuthProvider
from nexus.lib.oauth.providers.microsoft import MicrosoftOAuthProvider
from nexus.lib.oauth.providers.slack import SlackOAuthProvider
from nexus.lib.oauth.providers.x import XOAuthProvider

__all__ = [
    "GoogleOAuthProvider",
    "MicrosoftOAuthProvider",
    "SlackOAuthProvider",
    "XOAuthProvider",
]
