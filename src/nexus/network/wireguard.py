"""WireGuard tunnel management for Nexus federation.

Linux analogy: ``net/wireguard/`` — creates encrypted tunnels between nodes.

Manages WireGuard keypairs, peer configuration, and tunnel lifecycle via
the ``wg`` and ``wg-quick`` CLI tools.  This module does NOT implement
WireGuard itself — it orchestrates the external tools.

IP scheme: ``10.99.0.{node_id}/24`` — avoids common LAN ranges.

Prerequisites:
    Windows: Install from https://www.wireguard.com/install/
    macOS:   brew install wireguard-tools
    Linux:   apt install wireguard-tools

Example:
    >>> from nexus.network.wireguard import init_identity, add_peer, tunnel_up
    >>> identity = init_identity(node_id=1)
    >>> print(f"Public key: {identity['public_key']}")
    >>> add_peer(node_id=2, public_key="...", endpoint="192.168.1.50:51820")
    >>> tunnel_up()
"""

import json
import logging
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from nexus.network.constants import WG_DEFAULT_PORT, WG_INTERFACE, WG_SUBNET

logger = logging.getLogger(__name__)

NETWORK_DIR = Path.home() / ".nexus" / "network"
PEERS_DIR = NETWORK_DIR / "peers"


# =============================================================================
# Key Generation
# =============================================================================


def _find_wg() -> str:
    """Find the ``wg`` executable.  Returns the path or raises RuntimeError."""
    wg = shutil.which("wg")
    if wg:
        return wg
    # Windows: installer puts wg.exe here but may not update PATH for current session
    if platform.system() == "Windows":
        fallback = r"C:\Program Files\WireGuard\wg.exe"
        if Path(fallback).exists():
            return fallback
    raise RuntimeError(
        "WireGuard CLI (wg) not found in PATH.\n"
        "Install: https://www.wireguard.com/install/\n"
        "  Windows: winget install WireGuard.WireGuard\n"
        "  macOS:   brew install wireguard-tools\n"
        "  Linux:   apt install wireguard-tools"
    )


def generate_keypair() -> tuple[str, str]:
    """Generate a WireGuard keypair using ``wg genkey`` + ``wg pubkey``.

    Returns:
        (private_key, public_key) as base64 strings.
    """
    wg = _find_wg()
    privkey = subprocess.check_output([wg, "genkey"], text=True).strip()
    pubkey = subprocess.check_output([wg, "pubkey"], input=privkey, text=True).strip()
    return privkey, pubkey


# =============================================================================
# Identity & Peer Persistence
# =============================================================================


def get_node_ip(node_id: int) -> str:
    """Get WireGuard IP for a node.  ``10.99.0.{node_id}``."""
    if not 1 <= node_id <= 254:
        raise ValueError(f"node_id must be 1-254, got {node_id}")
    return f"{WG_SUBNET}.{node_id}"


def init_identity(node_id: int, listen_port: int = WG_DEFAULT_PORT) -> dict:
    """Generate and save WireGuard identity for this node.

    Creates ``~/.nexus/network/identity.json`` with keypair + config.

    Returns:
        Identity dict with node_id, private_key, public_key, listen_port, ip.
    """
    NETWORK_DIR.mkdir(parents=True, exist_ok=True)
    PEERS_DIR.mkdir(parents=True, exist_ok=True)

    identity_path = NETWORK_DIR / "identity.json"
    if identity_path.exists():
        logger.warning("Identity already exists at %s — overwriting", identity_path)

    privkey, pubkey = generate_keypair()
    identity = {
        "node_id": node_id,
        "private_key": privkey,
        "public_key": pubkey,
        "listen_port": listen_port,
        "ip": get_node_ip(node_id),
    }
    identity_path.write_text(json.dumps(identity, indent=2))
    logger.info("Identity saved: node=%d ip=%s", node_id, identity["ip"])
    return identity


def load_identity() -> dict:
    """Load saved identity from ``~/.nexus/network/identity.json``.

    Raises:
        FileNotFoundError: If identity not yet initialized.
    """
    identity_path = NETWORK_DIR / "identity.json"
    if not identity_path.exists():
        raise FileNotFoundError(
            f"No identity found at {identity_path}. Run `nexus network init` first."
        )
    return json.loads(identity_path.read_text())


def add_peer(node_id: int, public_key: str, endpoint: str) -> dict:
    """Save peer info to ``~/.nexus/network/peers/{node_id}.json``.

    Args:
        node_id: Peer's node ID (determines its WireGuard IP).
        public_key: Peer's WireGuard public key.
        endpoint: Peer's reachable address (``ip:port``).

    Returns:
        Peer dict with node_id, public_key, endpoint, ip.
    """
    PEERS_DIR.mkdir(parents=True, exist_ok=True)
    peer = {
        "node_id": node_id,
        "public_key": public_key,
        "endpoint": endpoint,
        "ip": get_node_ip(node_id),
    }
    peer_path = PEERS_DIR / f"{node_id}.json"
    peer_path.write_text(json.dumps(peer, indent=2))
    logger.info("Peer saved: node=%d ip=%s endpoint=%s", node_id, peer["ip"], endpoint)
    return peer


def load_peers() -> list[dict]:
    """Load all saved peers from ``~/.nexus/network/peers/``."""
    if not PEERS_DIR.exists():
        return []
    peers = []
    for path in sorted(PEERS_DIR.glob("*.json")):
        peers.append(json.loads(path.read_text()))
    return peers


def remove_peer(node_id: int) -> bool:
    """Remove a peer by node_id.  Returns True if removed."""
    peer_path = PEERS_DIR / f"{node_id}.json"
    if peer_path.exists():
        peer_path.unlink()
        return True
    return False


# =============================================================================
# Config Generation
# =============================================================================


def generate_wg_config(identity: dict, peers: list[dict]) -> str:
    """Generate a wg-quick config string.

    Args:
        identity: This node's identity (from ``load_identity()``).
        peers: List of peer dicts (from ``load_peers()``).

    Returns:
        wg-quick INI config string.
    """
    lines = [
        "[Interface]",
        f"PrivateKey = {identity['private_key']}",
        f"Address = {identity['ip']}/24",
        f"ListenPort = {identity['listen_port']}",
    ]

    for peer in peers:
        lines.extend(
            [
                "",
                "[Peer]",
                f"PublicKey = {peer['public_key']}",
                f"AllowedIPs = {peer['ip']}/32",
                f"Endpoint = {peer['endpoint']}",
                "PersistentKeepalive = 25",
            ]
        )

    return "\n".join(lines) + "\n"


# =============================================================================
# Tunnel Lifecycle
# =============================================================================


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _wg_quick_path() -> str:
    """Resolve wg-quick.  On Windows, use wireguard.exe /installtunnelservice."""
    if _is_windows():
        wg_exe = shutil.which("wireguard")
        if wg_exe:
            return wg_exe
        # Fallback: common install path
        fallback = r"C:\Program Files\WireGuard\wireguard.exe"
        if Path(fallback).exists():
            return fallback
        raise RuntimeError(
            "wireguard.exe not found. Install WireGuard from https://www.wireguard.com/install/"
        )
    wg_quick = shutil.which("wg-quick")
    if wg_quick is None:
        raise RuntimeError("wg-quick not found in PATH")
    return wg_quick


def tunnel_up(interface: str = WG_INTERFACE) -> str:
    """Bring up the WireGuard tunnel.

    Generates config from saved identity + peers, writes to temp file,
    and activates via wg-quick (Unix) or wireguard.exe (Windows).

    Returns:
        Status message.
    """
    identity = load_identity()
    peers = load_peers()
    if not peers:
        raise RuntimeError("No peers configured. Run `nexus network add-peer` first.")

    config = generate_wg_config(identity, peers)

    if _is_windows():
        return _tunnel_up_windows(config, interface)
    return _tunnel_up_unix(config, interface)


def _tunnel_up_unix(config: str, interface: str) -> str:
    """Activate tunnel on macOS/Linux via wg-quick."""
    config_path = Path(f"/etc/wireguard/{interface}.conf")

    # Write config (requires sudo)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
        f.write(config)
        tmp_path = f.name

    try:
        subprocess.run(
            ["sudo", "cp", tmp_path, str(config_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["sudo", "wg-quick", "up", interface],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to bring up tunnel: {e.stderr}") from e
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return f"Tunnel {interface} is up (IP: {load_identity()['ip']})"


def _tunnel_up_windows(config: str, interface: str) -> str:
    """Activate tunnel on Windows via wireguard.exe.

    Windows WireGuard uses a different flow:
    1. Write .conf to a known location
    2. Use ``wireguard.exe /installtunnelservice <conf_path>``
    """
    config_dir = NETWORK_DIR / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"{interface}.conf"
    config_path.write_text(config)

    wg_exe = _wg_quick_path()
    try:
        subprocess.run(
            [wg_exe, "/installtunnelservice", str(config_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to install tunnel service: {e.stderr}\n"
            "Ensure you are running as Administrator."
        ) from e

    return f"Tunnel {interface} installed (IP: {load_identity()['ip']})"


def tunnel_down(interface: str = WG_INTERFACE) -> str:
    """Tear down the WireGuard tunnel.

    Returns:
        Status message.
    """
    try:
        if _is_windows():
            wg_exe = _wg_quick_path()
            subprocess.run(
                [wg_exe, "/uninstalltunnelservice", interface],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            subprocess.run(
                ["sudo", "wg-quick", "down", interface],
                check=True,
                capture_output=True,
                text=True,
            )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to tear down tunnel: {e.stderr}") from e

    return f"Tunnel {interface} is down"


def tunnel_status() -> str:
    """Show WireGuard tunnel status via ``wg show``.

    Returns:
        Output of ``wg show``, or error message.
    """
    wg = _find_wg()
    try:
        if _is_windows():
            # On Windows, wg show may need elevated privileges
            result = subprocess.run(
                [wg, "show"],
                capture_output=True,
                text=True,
            )
        else:
            result = subprocess.run(
                ["sudo", wg, "show"],
                capture_output=True,
                text=True,
            )

        if result.returncode != 0:
            return f"No active tunnels (wg show returned: {result.stderr.strip()})"
        return result.stdout.strip() or "No active WireGuard interfaces"
    except FileNotFoundError:
        return "wg command not found"
