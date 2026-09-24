"""Moss membership revalidation for short-lived Zone delegations."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Literal

import httpx

MembershipState = Literal["ok", "inactive", "unreachable"]


class MembershipUnreachable(RuntimeError):
    """The authoritative Moss membership endpoint could not be consulted."""


@dataclass(frozen=True)
class _CachedMembership:
    status: str
    revision: int
    expires_at: float


class MossMembershipVerifier:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        cache_ttl_s: float = 0,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = url
        self._token = token
        self._cache_ttl_s = max(0.0, cache_ttl_s)
        self._client = client or httpx.Client(trust_env=False)
        self._cache: dict[tuple[str, str], _CachedMembership] = {}

    @classmethod
    def from_env(cls, *, cache_ttl_s: float = 0) -> MossMembershipVerifier | None:
        url = os.environ.get("NEXUS_ZONE_MEMBERSHIP_URL", "").strip()
        token = os.environ.get("NEXUS_ZONE_MEMBERSHIP_TOKEN", "").strip()
        if not url or not token:
            return None
        return cls(url, token, cache_ttl_s=cache_ttl_s)

    def _lookup(self, user_id: str, org_id: str) -> tuple[str, int]:
        key = (user_id, org_id)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > now:
            return cached.status, cached.revision
        try:
            response = self._client.get(
                self._url,
                params={"user_id": user_id, "org_id": org_id},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=2.0,
            )
            if response.status_code == 404:
                result = ("missing", -1)
            else:
                response.raise_for_status()
                body = response.json()
                result = (str(body["status"]), int(body["revision"]))
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise MembershipUnreachable("Moss membership lookup unavailable") from exc
        if self._cache_ttl_s > 0:
            self._cache[key] = _CachedMembership(
                status=result[0], revision=result[1], expires_at=now + self._cache_ttl_s
            )
        return result

    def check(self, user_id: str, org_id: str, membership_version: str) -> bool:
        status, revision = self._lookup(user_id, org_id)
        return status == "active" and membership_version == f"r{revision}"

    def check_detailed(self, user_id: str, org_id: str, membership_version: str) -> MembershipState:
        try:
            return "ok" if self.check(user_id, org_id, membership_version) else "inactive"
        except MembershipUnreachable:
            return "unreachable"
