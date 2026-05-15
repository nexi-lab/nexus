"""GitHub provider adapter — daemon-pushed `gh auth token` payload → MaterializedCredential (#3818).

Accepted payload shapes (daemon may push either):

  1. **Raw token bytes** — what the default daemon actually emits today: the
     stdout of ``gh auth token`` is a single line like ``ghp_xxx`` or
     ``github_pat_xxx``. No metadata is available (no scopes, no expiry).

  2. **JSON object** — preferred shape for richer metadata when the daemon
     wraps the token. Schema::

         {
           "token":      "ghp_..." | "github_pat_...",   # required
           "scopes":     ["repo", "read:user", ...],     # required (may be [])
           "expires_at": "ISO 8601",                     # optional (fine-grained PATs)
           "token_type": "classic" | "fine_grained"      # optional, defaults "classic"
         }

We try JSON first, then fall back to treating the bytes as a raw token.
This keeps the read path working with the existing daemon push without
forcing a daemon-side migration.
"""

from __future__ import annotations

import json
from datetime import datetime

from nexus.bricks.auth.consumer import MaterializedCredential

# Accepted PAT prefixes (classic + fine-grained + GitHub-issued tokens).
# Used to validate raw-bytes fallback so we don't materialize garbage
# (e.g. an empty file or HTML error page) as a credential.
_GITHUB_TOKEN_PREFIXES: tuple[str, ...] = (
    "ghp_",  # classic personal access token
    "github_pat_",  # fine-grained PAT
    "gho_",  # OAuth user-to-server
    "ghu_",  # GitHub App user-to-server
    "ghs_",  # GitHub App server-to-server
    "ghr_",  # refresh token
)


class GithubProviderAdapter:
    name: str = "github"

    def materialize(self, plaintext_payload: bytes) -> MaterializedCredential:
        text = plaintext_payload.decode("utf-8").strip()

        # Shape 2: JSON object with token + metadata (preferred).
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "token" in data:
                token = data["token"]
                scopes = data.get("scopes", [])
                token_type = data.get("token_type", "classic")
                expires_at: datetime | None = None
                if "expires_at" in data and data["expires_at"]:
                    expires_at = datetime.fromisoformat(data["expires_at"])
                return MaterializedCredential(
                    provider=self.name,
                    access_token=token,
                    expires_at=expires_at,
                    metadata={
                        "scopes_csv": ",".join(scopes),
                        "token_type": token_type,
                    },
                )
        except (json.JSONDecodeError, ValueError):
            pass  # fall through to raw-token shape

        # Shape 1: raw token bytes (default daemon push for `gh auth token`).
        # Validate prefix so we don't accept arbitrary plaintext as a credential.
        if not text.startswith(_GITHUB_TOKEN_PREFIXES):
            raise ValueError("payload is neither a recognized JSON shape nor a GitHub token prefix")
        return MaterializedCredential(
            provider=self.name,
            access_token=text,
            expires_at=None,
            metadata={"scopes_csv": "", "token_type": "classic"},
        )
