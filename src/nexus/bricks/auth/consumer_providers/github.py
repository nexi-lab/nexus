"""GitHub provider adapter — daemon-pushed `gh auth token` payload → MaterializedCredential (#3818).

Payload shape (JSON, daemon-pushed by `gh auth token`):
    {
      "token":      "ghp_..." | "github_pat_...",   # required
      "scopes":     ["repo", "read:user", ...],     # required (may be [])
      "expires_at": "ISO 8601",                     # optional (fine-grained PATs)
      "token_type": "classic" | "fine_grained"      # optional, defaults "classic"
    }
"""

from __future__ import annotations

import json
from datetime import datetime

from nexus.bricks.auth.consumer import MaterializedCredential


class GithubProviderAdapter:
    name: str = "github"

    def materialize(self, plaintext_payload: bytes) -> MaterializedCredential:
        data = json.loads(plaintext_payload.decode("utf-8"))
        token = data["token"]  # KeyError → AdapterMaterializeFailed
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
