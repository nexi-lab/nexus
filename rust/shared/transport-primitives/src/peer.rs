//! Peer addressing — hostname parsing and node ID derivation.

use crate::error::{Result, TransportError};

/// Derive a deterministic node ID from a hostname.
///
/// SHA-256 of hostname, first 8 bytes as little-endian u64.
/// Maps 0 to 1 (raft-rs reserves 0 as "no node").
///
/// This is the cold-start ID convention — every node in a freshly
/// bootstrapped cluster derives identical IDs for each named peer
/// from their hostnames alone, so initial `ConfState` is consistent
/// without coordination.  Wipe-rejoin paths use [`compute_node_id`]
/// with a non-zero incarnation to mint a fresh ID after a data wipe;
/// see `RaftStorage::set_incarnation` and `ensure_voter_membership`.
pub fn hostname_to_node_id(hostname: &str) -> u64 {
    compute_node_id(hostname, 0)
}

/// Derive a deterministic node ID from a hostname plus an incarnation.
///
/// `incarnation == 0` is the cold-start sentinel and returns the same
/// value as [`hostname_to_node_id`] — every node in a freshly bootstrapped
/// cluster sees the same IDs without exchanging incarnation values, so
/// the initial `ConfState` is consistent.
///
/// `incarnation > 0` mints a fresh ID space for that hostname:
/// SHA-256 of `"<hostname>:<incarnation>"` (incarnation as big-endian
/// u64), first 8 bytes as little-endian u64.  Used after a wipe-rejoin
/// where the leader's in-memory `Progress[old_id]` is stale and reusing
/// the prior ID would trigger raft-rs's `to_commit N out of range
/// [last_index 0]` panic in `handle_heartbeat`.
///
/// Same `0 → 1` mapping as [`hostname_to_node_id`] (raft-rs reserves 0
/// as "no node").
pub fn compute_node_id(hostname: &str, incarnation: u64) -> u64 {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(hostname.as_bytes());
    if incarnation != 0 {
        // Suffix only when non-zero so cold-start path matches the
        // pre-incarnation `hostname_to_node_id` byte-for-byte.
        hasher.update(b":");
        hasher.update(incarnation.to_be_bytes());
    }
    let hash = hasher.finalize();
    let mut first_eight = [0u8; 8];
    first_eight.copy_from_slice(&hash[..8]);
    let value = u64::from_le_bytes(first_eight);
    if value == 0 {
        1
    } else {
        value
    }
}

fn format_host_port(hostname: &str, port: u16) -> String {
    let needs_brackets =
        hostname.contains(':') && !hostname.starts_with('[') && !hostname.ends_with(']');
    if needs_brackets {
        format!("[{}]:{}", hostname, port)
    } else {
        format!("{}:{}", hostname, port)
    }
}

#[allow(clippy::result_large_err)]
fn parse_host_port(addr: &str, original: &str) -> Result<(String, u16)> {
    let (hostname, port_str) = if let Some(rest) = addr.strip_prefix('[') {
        let Some(close_idx) = rest.find(']') else {
            return Err(TransportError::InvalidAddress(format!(
                "missing closing ']' in '{}'",
                original
            )));
        };
        let hostname = &rest[..close_idx];
        let remainder = &rest[close_idx + 1..];
        let Some(port_str) = remainder.strip_prefix(':') else {
            return Err(TransportError::InvalidAddress(format!(
                "expected ':port' after host in '{}'",
                original
            )));
        };
        (hostname, port_str)
    } else {
        let Some((hostname, port_str)) = addr.rsplit_once(':') else {
            return Err(TransportError::InvalidAddress(format!(
                "expected 'host:port', got '{}'",
                original
            )));
        };

        // Require bracketed IPv6 to avoid ambiguous host/port parsing.
        if hostname.contains(':') {
            return Err(TransportError::InvalidAddress(format!(
                "IPv6 addresses must be bracketed: '{}'",
                original
            )));
        }
        (hostname, port_str)
    };

    let hostname = hostname.trim();
    if hostname.is_empty() {
        return Err(TransportError::InvalidAddress(format!(
            "host cannot be empty: '{}'",
            original
        )));
    }

    let port: u16 = port_str.parse().map_err(|_| {
        TransportError::InvalidAddress(format!("invalid port in '{}': '{}'", original, port_str))
    })?;
    if port == 0 {
        return Err(TransportError::InvalidAddress(format!(
            "port must be 1-65535 in '{}'",
            original
        )));
    }

    Ok((hostname.to_string(), port))
}

/// Address of a network peer (Raft node, gRPC endpoint).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct PeerAddress {
    /// Peer hostname (e.g., "nexus-1").
    pub hostname: String,
    /// Peer port (e.g., 2126).
    pub port: u16,
    /// Node ID (derived from hostname via SHA-256).
    pub id: u64,
    /// gRPC endpoint (e.g., "http://nexus-1:2126").
    pub endpoint: String,
}

impl PeerAddress {
    /// Create a new PeerAddress with explicit id and endpoint.
    pub fn new(id: u64, endpoint: impl Into<String>) -> Self {
        let endpoint = endpoint.into();
        Self {
            hostname: String::new(),
            port: 0,
            id,
            endpoint,
        }
    }

    /// Parse from "host:port" or "id@host:port" format, deriving node_id from hostname.
    #[allow(clippy::result_large_err)]
    pub fn parse(s: &str, use_tls: bool) -> Result<Self> {
        let s = s.trim();
        let addr = s
            .strip_prefix("http://")
            .or_else(|| s.strip_prefix("https://"))
            .unwrap_or(s);

        // Strip "id@" prefix if present
        let addr = match addr.find('@') {
            Some(pos) => &addr[pos + 1..],
            None => addr,
        };

        let (hostname, port) = parse_host_port(addr, s)?;
        let id = hostname_to_node_id(&hostname);

        let scheme = if use_tls { "https" } else { "http" };
        let endpoint = format!("{}://{}", scheme, format_host_port(&hostname, port));

        Ok(Self {
            hostname,
            port,
            id,
            endpoint,
        })
    }

    /// Parse a comma-separated list of "host:port" peers.
    #[allow(clippy::result_large_err)]
    pub fn parse_peer_list(s: &str, use_tls: bool) -> Result<Vec<Self>> {
        s.split(',')
            .filter(|p| !p.trim().is_empty())
            .map(|p| Self::parse(p.trim(), use_tls))
            .collect()
    }

    /// Return "host:port" for gRPC connection target.
    pub fn grpc_target(&self) -> String {
        if self.hostname.is_empty() {
            self.endpoint
                .trim_start_matches("http://")
                .trim_start_matches("https://")
                .to_string()
        } else {
            format_host_port(&self.hostname, self.port)
        }
    }

    /// Return "id@host:port" for Raft peer configuration.
    pub fn to_raft_peer_str(&self) -> String {
        format!("{}@{}", self.id, self.grpc_target())
    }
}

/// Backward-compatible type alias.
pub type NodeAddress = PeerAddress;

impl std::fmt::Display for PeerAddress {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}@{}", self.id, self.endpoint)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_hostname_to_node_id_golden_values() {
        assert_eq!(hostname_to_node_id("nexus-1"), 14044926161142285152);
        assert_eq!(hostname_to_node_id("nexus-2"), 768242927742468745);
        assert_eq!(hostname_to_node_id("witness"), 10099512703796518074);
    }

    #[test]
    fn test_compute_node_id_zero_incarnation_matches_hostname_only() {
        // Cold-start sentinel: incarnation=0 must equal the prior
        // hostname-only function byte-for-byte so existing deployments
        // and the cold-start convergence path keep working unchanged.
        for h in ["nexus-1", "nexus-2", "witness", "100.64.0.21"] {
            assert_eq!(compute_node_id(h, 0), hostname_to_node_id(h));
        }
    }

    #[test]
    fn test_compute_node_id_nonzero_incarnation_differs_from_hostname_only() {
        // After a wipe the same hostname must produce a *different* ID
        // when paired with a non-zero incarnation — that's the whole
        // point of the marker.
        let host = "nexus-1";
        let cold = hostname_to_node_id(host);
        let fresh1 = compute_node_id(host, 1);
        let fresh_max = compute_node_id(host, u64::MAX);
        assert_ne!(fresh1, cold);
        assert_ne!(fresh_max, cold);
        assert_ne!(fresh1, fresh_max);
    }

    #[test]
    fn test_compute_node_id_deterministic_per_input() {
        // Same inputs → same output across calls.
        for inc in [0u64, 1, 42, u64::MAX] {
            assert_eq!(
                compute_node_id("nexus-1", inc),
                compute_node_id("nexus-1", inc)
            );
        }
    }

    #[test]
    fn test_compute_node_id_avoids_zero() {
        // raft-rs reserves 0 as "no node"; the helper must never return 0.
        // Spot-check a range of incarnations to make the property visible.
        for inc in 0..1000u64 {
            assert_ne!(compute_node_id("nexus-1", inc), 0);
            assert_ne!(compute_node_id("witness", inc), 0);
        }
    }

    #[test]
    fn test_peer_address_parse() {
        let addr = PeerAddress::parse("nexus-1:2126", false).unwrap();
        assert_eq!(addr.hostname, "nexus-1");
        assert_eq!(addr.port, 2126);
        assert_eq!(addr.id, hostname_to_node_id("nexus-1"));
        assert_eq!(addr.endpoint, "http://nexus-1:2126");
    }

    #[test]
    fn test_peer_address_parse_tls() {
        let addr = PeerAddress::parse("nexus-2:2126", true).unwrap();
        assert_eq!(addr.endpoint, "https://nexus-2:2126");
    }

    #[test]
    fn test_peer_address_parse_ipv6() {
        let addr = PeerAddress::parse("[::1]:2126", false).unwrap();
        assert_eq!(addr.hostname, "::1");
        assert_eq!(addr.port, 2126);
        assert_eq!(addr.endpoint, "http://[::1]:2126");
        assert_eq!(addr.grpc_target(), "[::1]:2126");
    }

    #[test]
    fn test_peer_address_parse_rejects_invalid_host_port() {
        assert!(matches!(
            PeerAddress::parse("2001:db8::1:2126", false),
            Err(TransportError::InvalidAddress(_))
        ));
        assert!(matches!(
            PeerAddress::parse(":2126", false),
            Err(TransportError::InvalidAddress(_))
        ));
        assert!(matches!(
            PeerAddress::parse("nexus-1:0", false),
            Err(TransportError::InvalidAddress(_))
        ));
    }
}
