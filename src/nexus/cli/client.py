"""HTTP client for Nexus REST API — used by non-filesystem CLI commands.

Provides a thin wrapper around httpx for calling /api/v2/* endpoints
that don't map to the NexusFS abstraction (pay, audit, locks, governance,
events, snapshots).
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class NexusAPIError(Exception):
    """Error from Nexus REST API."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class NexusServiceClient:
    """HTTP client for Nexus service-level REST endpoints.

    Usage::

        with NexusServiceClient(url, api_key) as client:
            balance = client.pay_balance()
            client.pay_transfer("bob", "10.00")
    """

    def __init__(self, url: str, api_key: str | None = None) -> None:
        import httpx  # Lazy import — heavy dependency

        self._url = url.rstrip("/")
        self._api_key = api_key
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(base_url=self._url, headers=headers, timeout=30.0)

    def __enter__(self) -> "NexusServiceClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self._client.close()

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make HTTP request and return parsed JSON response."""
        # Filter None values from params
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        response = self._client.request(method, path, params=params, json=json_body)

        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise NexusAPIError(response.status_code, str(detail))

        if response.status_code == 204:
            return {}

        result: dict[str, Any] = response.json()
        return result

    # ----- Pay -----

    def pay_balance(self, agent_id: str | None = None) -> dict[str, Any]:
        params = {"agent_id": agent_id} if agent_id else None
        return self._request("GET", "/api/v2/pay/balance", params=params)

    def pay_transfer(
        self,
        to: str,
        amount: str,
        memo: str = "",
        method: str = "auto",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v2/pay/transfer",
            json_body={
                "to": to,
                "amount": amount,
                "memo": memo,
                "method": method,
            },
        )

    def pay_history(
        self,
        since: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/api/v2/audit/transactions",
            params={
                "since": since,
                "limit": limit,
                "cursor": cursor,
            },
        )

    # ----- Audit -----

    def audit_list(
        self,
        since: str | None = None,
        until: str | None = None,
        agent_id: str | None = None,
        action: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/api/v2/audit/transactions",
            params={
                "since": since,
                "until": until,
                "agent_id": agent_id,
                "status": action,
                "limit": limit,
                "cursor": cursor,
            },
        )

    def audit_export(
        self,
        fmt: str = "json",
        since: str | None = None,
        until: str | None = None,
    ) -> str:
        """Export audit data — returns raw response text."""
        params: dict[str, Any] = {"format": fmt}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        params = {k: v for k, v in params.items() if v is not None}

        response = self._client.request("GET", "/api/v2/audit/transactions/export", params=params)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise NexusAPIError(response.status_code, str(detail))
        return response.text

    # ----- Locks -----

    def lock_list(self, zone_id: str | None = None) -> dict[str, Any]:
        return self._request("GET", "/api/v2/locks", params={"zone_id": zone_id})

    def lock_info(self, path: str) -> dict[str, Any]:
        path = path.lstrip("/")
        return self._request("GET", f"/api/v2/locks/{path}")

    def lock_release(
        self, path: str, lock_id: str | None = None, force: bool = False
    ) -> dict[str, Any]:
        path = path.lstrip("/")
        params: dict[str, Any] = {}
        if lock_id:
            params["lock_id"] = lock_id
        if force:
            params["force"] = "true"
        return self._request("DELETE", f"/api/v2/locks/{path}", params=params)

    # ----- Governance -----

    def governance_alerts(
        self,
        severity: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/api/v2/governance/alerts",
            params={
                "severity": severity,
                "since": since,
                "limit": limit,
            },
        )

    def governance_status(self) -> dict[str, Any]:
        """Get governance overview (alerts + fraud scores summary)."""
        alerts = self._request("GET", "/api/v2/governance/alerts", params={"limit": 5})
        rings = self._request("GET", "/api/v2/governance/rings")
        return {"recent_alerts": alerts, "fraud_rings": rings}

    def governance_rings(self) -> dict[str, Any]:
        return self._request("GET", "/api/v2/governance/rings")

    # ----- Events -----

    def events_replay(
        self,
        since: str | None = None,
        event_type: str | None = None,
        path: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/api/v2/events/replay",
            params={
                "since": since,
                "event_type": event_type,
                "path": path,
                "limit": limit,
            },
        )

    def events_subscribe(self, pattern: str) -> str:
        """Subscribe to events via SSE — returns raw response text."""
        response = self._client.request(
            "GET",
            "/api/v2/events/stream",
            params={"pattern": pattern},
            headers={"Accept": "text/event-stream"},
        )
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise NexusAPIError(response.status_code, str(detail))
        return response.text

    # ----- Federation -----

    def federation_zones(self) -> dict[str, Any]:
        return self._request("GET", "/api/v2/federation/zones")

    def federation_cluster_info(self, zone_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v2/federation/zones/{zone_id}/cluster-info")

    def federation_share(self, local_path: str, zone_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"local_path": local_path}
        if zone_id:
            body["zone_id"] = zone_id
        return self._request("POST", "/api/v2/federation/share", json_body=body)

    def federation_join(self, peer_addr: str, remote_path: str, local_path: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v2/federation/join",
            json_body={
                "peer_addr": peer_addr,
                "remote_path": remote_path,
                "local_path": local_path,
            },
        )

    def federation_mount(self, parent_zone: str, path: str, target_zone: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v2/federation/mounts",
            json_body={
                "parent_zone": parent_zone,
                "path": path,
                "target_zone": target_zone,
            },
        )

    def federation_unmount(self, parent_zone: str, path: str) -> dict[str, Any]:
        return self._request(
            "DELETE",
            "/api/v2/federation/mounts",
            params={"parent_zone": parent_zone, "path": path},
        )

    # ----- Snapshots -----

    def snapshot_create(
        self, description: str | None = None, ttl_seconds: int = 3600
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"ttl_seconds": ttl_seconds}
        if description:
            body["description"] = description
        return self._request("POST", "/api/v2/snapshots", json_body=body)

    def snapshot_list(self) -> dict[str, Any]:
        return self._request("GET", "/api/v2/snapshots")

    def snapshot_restore(self, txn_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v2/snapshots/{txn_id}/rollback")
