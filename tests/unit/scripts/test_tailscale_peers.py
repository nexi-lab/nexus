"""Unit tests for ``scripts/_tailscale_peers.py``.

Mocks the ``tailscale status --json`` subprocess so the resolver's
prefix-matching, online-filtering, and error paths can be pinned
without requiring a live Tailscale install on the test runner.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts import _tailscale_peers as tp

# Fixture mirroring the real ``tailscale status --json`` shape that
# surfaced during the post-SSD-swap recovery — one offline stale
# ``songym-win`` plus a current ``songym-win-ipwn5jf2`` we registered
# under the same base hostname.
_STATUS_FIXTURE = {
    "Self": {
        "HostName": "songym-win-ipwn5jf2",
        "DNSName": "songym-win-ipwn5jf2.nexus.tailscale.sudoprivacy.com.",
        "TailscaleIPs": ["100.64.0.27", "fd7a:115c:a1e0::4d01"],
        "Online": True,
    },
    "Peer": {
        "node-stale": {
            "HostName": "songym-win",
            "DNSName": "songym-win.nexus.tailscale.sudoprivacy.com.",
            "TailscaleIPs": ["100.64.0.26"],
            "Online": False,
        },
        "node-mac-current": {
            "HostName": "huxt-mac",
            "DNSName": "huxt-mac.nexus.tailscale.sudoprivacy.com.",
            "TailscaleIPs": ["100.64.0.24"],
            "Online": True,
        },
        "node-mac-stale": {
            "HostName": "huxt-mac-oldssd",
            "DNSName": "huxt-mac-oldssd.nexus.tailscale.sudoprivacy.com.",
            "TailscaleIPs": ["100.64.0.23"],
            "Online": False,
        },
        "node-linux": {
            "HostName": "huxuetao-linux",
            "DNSName": "huxuetao-linux.sudo.tailscale.sudoprivacy.com.",
            "TailscaleIPs": ["100.64.0.14"],
            "Online": True,
        },
    },
}


@pytest.fixture
def fake_status():
    """Patch ``_tailscale_status_json`` to return the fixture above."""
    with patch.object(tp, "_tailscale_status_json", return_value=_STATUS_FIXTURE):
        yield


def test_resolve_self_ipv4_only(fake_status):
    # The Self block carries both IPv4 and IPv6; we only return IPv4
    # because federation peer strings universally use the IPv4 100.64/10
    # CGNAT space.
    assert tp.resolve_self_ip() == "100.64.0.27"


def test_resolve_peer_exact_match(fake_status):
    # Exact hostname match on a Peer block — no stale collision, no
    # filtering noise.
    assert tp.resolve_peer("huxt-mac") == "100.64.0.24"


def test_resolve_peer_skips_offline_stale(fake_status):
    # Two peers share the ``huxt-mac`` prefix: the current Online one
    # and a stale offline one carrying the same family of hostnames.
    # online_only=True (default) must drop the stale entry so the
    # caller cannot accidentally point federation at a dead IP.
    assert tp.resolve_peer("huxt-mac") == "100.64.0.24"


def test_resolve_peer_unique_prefix(fake_status):
    # A prefix that matches only one Online peer resolves cleanly.
    assert tp.resolve_peer("huxuetao") == "100.64.0.14"


def test_resolve_peer_missing_raises(fake_status):
    # Fail loud rather than fall back to a stale or wrong address —
    # callers driving federation tests need the error surface to know
    # the operator must intervene (delete stale node, re-up, etc.).
    with pytest.raises(tp.PeerNotFoundError) as exc:
        tp.resolve_peer("no-such-host")
    msg = str(exc.value)
    assert "no-such-host" in msg
    # Error message must list available peers so the operator has
    # something actionable to diagnose with.
    assert "huxt-mac" in msg


def test_resolve_peer_only_match_is_offline_raises(fake_status):
    # ``songym-win`` matches only the stale offline node from Peer;
    # Self block (``songym-win-ipwn5jf2``) is not searched because the
    # resolver intentionally splits resolve_self_ip() from resolve_peer()
    # — different concerns, different APIs.
    with pytest.raises(tp.PeerNotFoundError):
        tp.resolve_peer("songym-win")


def test_resolve_peer_offline_allowed_when_flag_off(fake_status):
    # ``online_only=False`` is the escape hatch for diagnostics
    # (e.g. operator inspecting last-seen IP of a powered-down node).
    # Single offline match should resolve.
    assert tp.resolve_peer("songym-win", online_only=False) == "100.64.0.26"


def test_resolve_peer_ambiguous_match_raises(fake_status):
    # Two Online peers match the prefix ``huxt`` (huxt-mac + the stale
    # one is filtered out, but if we relax online_only both match).
    # Ambiguity must error loud so callers don't silently pick one.
    with pytest.raises(tp.PeerNotFoundError) as exc:
        tp.resolve_peer("huxt", online_only=False)
    assert "Multiple" in str(exc.value) or "multiple" in str(exc.value).lower()


def test_resolve_self_ip_no_ipv4_raises():
    # Pathological: Tailscale up but Self has no IPv4 (IPv6-only).
    # Resolver must fail loud — federation today is IPv4-only.
    fixture = {
        "Self": {
            "HostName": "ipv6-only",
            "TailscaleIPs": ["fd7a:115c:a1e0::4d01"],
            "Online": True,
        },
        "Peer": {},
    }
    with (
        patch.object(tp, "_tailscale_status_json", return_value=fixture),
        pytest.raises(tp.PeerNotFoundError),
    ):
        tp.resolve_self_ip()
