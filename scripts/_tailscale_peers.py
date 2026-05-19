"""Tailscale peer resolver — hostname → current Tailscale IP at runtime.

Federation smoke scripts on Win + Mac historically hardcoded the
Tailscale IPs (e.g. ``100.64.0.26`` for Win, ``100.64.0.21`` for Mac).
That coupling broke whenever Headscale reassigned IPs — typical
triggers being SSD swap, OS reinstall, or any device migration that
made the node key disappear locally so Headscale registered a fresh
node and gave it a new IP.

The robust + hands-free fix is to treat the **Tailscale hostname** as
the canonical identifier and resolve the IP at runtime via
``tailscale status --json``.  Hostnames are stable across re-joins
(Headscale appends a random suffix only when the exact base name is
already taken by an offline-but-not-deleted node, but the prefix the
caller passes still matches).  Online filtering rejects stale entries
so prefix collisions with old offline devices don't accidentally
resolve to a dead IP.

Canonical hostnames the team commits to:

  * Win side: ``songym-win`` (or any unique prefix the operator picks
    via ``tailscale up --hostname=...``).
  * Mac side: ``huxt-mac`` (likewise).

If a side's hostname differs from the default, override at the call
site or set the matching env var documented by the caller.

The resolver shells out to the ``tailscale`` CLI — no SDK dep, works
on every platform the binary supports, and degrades gracefully when
Tailscale is not installed (raises ``RuntimeError`` with an actionable
message rather than masking it).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterable


class TailscaleNotInstalledError(RuntimeError):
    """Raised when the ``tailscale`` CLI is not on PATH or fails to run."""


class PeerNotFoundError(RuntimeError):
    """Raised when no Tailscale peer matches the requested hostname pattern."""


def _tailscale_bin() -> str:
    """Locate the ``tailscale`` binary on the current platform."""
    found = shutil.which("tailscale")
    if found:
        return found
    # Windows install path is not on PATH by default.
    win_default = r"C:\Program Files\Tailscale\tailscale.exe"
    if os.path.exists(win_default):
        return win_default
    raise TailscaleNotInstalledError(
        "tailscale CLI not found on PATH or at the standard Windows install "
        "location. Install Tailscale from https://tailscale.com/download "
        "and re-run."
    )


def _tailscale_status_json() -> dict:
    """Return parsed output of ``tailscale status --json``."""
    bin_path = _tailscale_bin()
    try:
        out = subprocess.check_output(
            [bin_path, "status", "--json"],
            stderr=subprocess.STDOUT,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise TailscaleNotInstalledError(f"`tailscale status --json` failed: {exc}") from exc
    return json.loads(out)


def resolve_self_ip() -> str:
    """Return this node's primary Tailscale IPv4 address."""
    data = _tailscale_status_json()
    self_node = data.get("Self") or {}
    ips: list[str] = list(self_node.get("TailscaleIPs") or [])
    ipv4 = [ip for ip in ips if ":" not in ip]
    if not ipv4:
        raise PeerNotFoundError(
            "Self has no IPv4 Tailscale address — is Tailscale up? "
            "Run `tailscale status` to diagnose."
        )
    return ipv4[0]


def resolve_peer(hostname_pattern: str, *, online_only: bool = True) -> str:
    """Resolve a peer's Tailscale IP by hostname pattern.

    Matches against ``HostName`` (the Tailscale-given name, which may
    carry a random suffix if Headscale renamed at registration) and
    ``DNSName`` using exact-or-prefix match.  When multiple peers
    match (e.g. a stale offline ``songym-win`` plus the current
    ``songym-win-ipwn5jf2``), ``online_only=True`` keeps only the
    Online peer.

    Raises ``PeerNotFoundError`` if zero matches survive the filter —
    callers are expected to fail loud rather than silently fall back
    to a stale address.
    """
    data = _tailscale_status_json()
    candidates: list[tuple[str, dict]] = []
    for node in (data.get("Peer") or {}).values():
        host = (node.get("HostName") or "").lower()
        dns = (node.get("DNSName") or "").lower()
        pat = hostname_pattern.lower()
        if host == pat or host.startswith(pat) or dns.startswith(pat):
            candidates.append((host or dns, node))

    if online_only:
        candidates = [(h, n) for (h, n) in candidates if n.get("Online")]

    if not candidates:
        raise PeerNotFoundError(
            f"No Tailscale peer matching '{hostname_pattern}' "
            f"(online_only={online_only}). Available peers: "
            f"{_peer_summary(data)}"
        )

    if len(candidates) > 1:
        names = ", ".join(h for h, _ in candidates)
        raise PeerNotFoundError(
            f"Multiple Tailscale peers match '{hostname_pattern}' "
            f"({names}). Use a more specific prefix."
        )

    node = candidates[0][1]
    ipv4 = [ip for ip in (node.get("TailscaleIPs") or []) if ":" not in ip]
    if not ipv4:
        raise PeerNotFoundError(f"Peer '{hostname_pattern}' has no IPv4 Tailscale address.")
    return ipv4[0]


def _peer_summary(data: dict) -> str:
    """Compact one-liner of (host, ip, online) tuples for error messages."""
    rows: list[str] = []
    for node in (data.get("Peer") or {}).values():
        host = node.get("HostName") or "?"
        ips = node.get("TailscaleIPs") or []
        ipv4 = next((ip for ip in ips if ":" not in ip), "?")
        online = "online" if node.get("Online") else "offline"
        rows.append(f"{host}={ipv4}({online})")
    return "; ".join(rows) if rows else "<no peers>"


def main(argv: Iterable[str] | None = None) -> int:
    """CLI for ad-hoc lookups, e.g. ``python -m scripts._tailscale_peers self``."""
    import sys

    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in {"-h", "--help"}:
        print(__doc__)
        print("\nUsage:")
        print("  python -m scripts._tailscale_peers self")
        print("  python -m scripts._tailscale_peers peer <hostname-prefix>")
        return 0

    cmd = args[0]
    if cmd == "self":
        print(resolve_self_ip())
        return 0
    if cmd == "peer":
        if len(args) < 2:
            print("error: peer requires a hostname prefix")
            return 2
        print(resolve_peer(args[1]))
        return 0
    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
