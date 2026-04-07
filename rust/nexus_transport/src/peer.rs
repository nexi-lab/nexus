//! Peer addressing — hostname parsing and node ID derivation.

use crate::error::{Result, TransportError};

/// Derive a deterministic node ID from a hostname.
///
/// SHA-256 of hostname, first 8 bytes as little-endian u64.
/// Maps 0 to 1 (raft-rs reserves 0 as "no node").
pub fn hostname_to_node_id(hostname: &str) -> u64 {
    use sha2::{Digest, Sha256};
    let hash = Sha256::digest(hostname.as_bytes());
    let value = u64::from_le_bytes(hash[..8].try_into().unwrap());
    if value == 0 {
        1
    } else {
        value
    }
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

        let parts: Vec<&str> = addr.rsplitn(2, ':').collect();
        if parts.len() != 2 {
            return Err(TransportError::InvalidAddress(format!(
                "expected 'host:port', got '{}'",
                s
            )));
        }

        let port: u16 = parts[0]
            .parse()
            .map_err(|_| TransportError::InvalidAddress(format!("invalid port: '{}'", parts[0])))?;
        let hostname = parts[1].to_string();
        let id = hostname_to_node_id(&hostname);

        let scheme = if use_tls { "https" } else { "http" };
        let endpoint = format!("{}://{}:{}", scheme, hostname, port);

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
            format!("{}:{}", self.hostname, self.port)
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
}
