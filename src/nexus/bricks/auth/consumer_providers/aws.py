"""AWS provider adapter — daemon-pushed STS payload → MaterializedCredential (#3818).

Payload shape (JSON, daemon-pushed by aws sso login / aws sts get-caller-identity):
    {
      "access_key_id":     "ASIA...",       # required
      "secret_access_key": "...",           # required
      "session_token":     "...",           # required (the time-bounded part)
      "expiration":        "ISO 8601",      # required (UTC)
      "region":            "us-...",        # required
      "account_id":        "123456789012"   # optional
    }

The wire response carries ``session_token`` in ``access_token`` and the rest
in ``nexus_credential_metadata``. Caller-side SDK builds ``boto3.Session``
from the pair.
"""

from __future__ import annotations

import json
from datetime import datetime

from nexus.bricks.auth.consumer import MaterializedCredential


class AwsProviderAdapter:
    name: str = "aws"

    def materialize(self, plaintext_payload: bytes) -> MaterializedCredential:
        data = json.loads(plaintext_payload.decode("utf-8"))
        # Required fields — KeyError surfaces as AdapterMaterializeFailed in the consumer.
        session_token = data["session_token"]
        access_key_id = data["access_key_id"]
        secret_access_key = data["secret_access_key"]
        expiration_iso = data["expiration"]
        region = data["region"]

        expires_at = datetime.fromisoformat(expiration_iso)

        metadata = {
            "access_key_id": access_key_id,
            "secret_access_key": secret_access_key,
            "region": region,
        }
        if "account_id" in data:
            metadata["account_id"] = data["account_id"]

        return MaterializedCredential(
            provider=self.name,
            access_token=session_token,
            expires_at=expires_at,
            metadata=metadata,
        )
