//! Server-side node certificate generation for JoinCluster RPC.
//!
//! Generates X.509 node certificates signed by the cluster CA, matching
//! the output of Python `certgen.py`: EC P-256, SHA-256, mTLS-ready SANs.
//!
//! The CA private key never leaves node-1 — this module is called server-side
//! during JoinCluster to sign certs for joining nodes.

use rcgen::{
    CertificateParams, DistinguishedName, DnType, ExtendedKeyUsagePurpose, IsCa, KeyPair,
    KeyUsagePurpose, SanType, PKCS_ECDSA_P256_SHA256,
};
use std::net::{Ipv4Addr, Ipv6Addr};

/// Generate a node certificate signed by the cluster CA.
///
/// Returns `(node_cert_pem, node_key_pem)` as PEM-encoded bytes.
///
/// The certificate matches Python `certgen.py` output:
/// - Algorithm: EC P-256 (ECDSA with SHA-256)
/// - CN: `nexus-zone-{zone_id}-node-{node_id}`
/// - SANs: localhost, 127.0.0.1, ::1
/// - Extended Key Usage: serverAuth + clientAuth (mTLS)
/// - Validity: 365 days
pub fn generate_node_cert(
    node_id: u64,
    zone_id: &str,
    ca_cert_pem: &[u8],
    ca_key_pem: &[u8],
    extra_hostnames: &[String],
    hostname: Option<&str>,
) -> Result<(Vec<u8>, Vec<u8>), String> {
    // Parse CA key pair
    let ca_key_str =
        std::str::from_utf8(ca_key_pem).map_err(|e| format!("CA key is not valid UTF-8: {e}"))?;
    let ca_key_pair =
        KeyPair::from_pem(ca_key_str).map_err(|e| format!("Failed to parse CA key: {e}"))?;

    // Parse CA certificate
    let ca_cert_str =
        std::str::from_utf8(ca_cert_pem).map_err(|e| format!("CA cert is not valid UTF-8: {e}"))?;
    let ca_cert_params = CertificateParams::from_ca_cert_pem(ca_cert_str)
        .map_err(|e| format!("Failed to parse CA cert: {e}"))?;
    let ca_cert = ca_cert_params
        .self_signed(&ca_key_pair)
        .map_err(|e| format!("Failed to reconstruct CA cert: {e}"))?;

    // Generate node key pair (EC P-256)
    let node_key_pair = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256)
        .map_err(|e| format!("Failed to generate node key: {e}"))?;

    // Build node certificate parameters
    let mut params = CertificateParams::default();

    // Distinguished name: CN=nexus-zone-{zone_id}-node-{hostname_or_id}, O=Nexus
    let cn_node = hostname.unwrap_or(&node_id.to_string()).to_string();
    let mut dn = DistinguishedName::new();
    dn.push(DnType::OrganizationName, "Nexus");
    dn.push(
        DnType::CommonName,
        format!("nexus-zone-{zone_id}-node-{cn_node}"),
    );
    params.distinguished_name = dn;

    // SANs: localhost, 127.0.0.1, ::1, plus any extra hostnames from node_address
    // (CockroachDB pattern: cert SANs include all hostnames the node is reachable at)
    let mut sans = vec![
        SanType::DnsName(
            "localhost"
                .try_into()
                .map_err(|e| format!("SAN error: {e}"))?,
        ),
        SanType::IpAddress(Ipv4Addr::LOCALHOST.into()),
        SanType::IpAddress(Ipv6Addr::LOCALHOST.into()),
    ];
    for hostname in extra_hostnames {
        // Try parsing as IP first, fall back to DNS name
        if let Ok(ip) = hostname.parse::<std::net::IpAddr>() {
            sans.push(SanType::IpAddress(ip));
        } else {
            sans.push(SanType::DnsName(
                hostname
                    .as_str()
                    .try_into()
                    .map_err(|e| format!("SAN error for '{hostname}': {e}"))?,
            ));
        }
    }
    params.subject_alt_names = sans;

    // Extended key usage: serverAuth + clientAuth (mTLS)
    params.extended_key_usages = vec![
        ExtendedKeyUsagePurpose::ServerAuth,
        ExtendedKeyUsagePurpose::ClientAuth,
    ];

    // Key usage
    params.key_usages = vec![
        KeyUsagePurpose::DigitalSignature,
        KeyUsagePurpose::KeyEncipherment,
    ];

    // Not a CA
    params.is_ca = IsCa::NoCa;

    // Validity: 365 days (matches Python default)
    let now = time::OffsetDateTime::now_utc();
    params.not_before = now;
    params.not_after = now + time::Duration::days(365);

    // Sign with CA
    let node_cert = params
        .signed_by(&node_key_pair, &ca_cert, &ca_key_pair)
        .map_err(|e| format!("Failed to sign node cert: {e}"))?;

    let cert_pem = node_cert.pem().into_bytes();
    let key_pem = node_key_pair.serialize_pem().into_bytes();

    Ok((cert_pem, key_pem))
}

/// Compute a SHA-256 fingerprint of a PEM-encoded CA certificate.
///
/// Returns the fingerprint in `SHA256:<base64-no-padding>` format,
/// matching the Python `cert_fingerprint()` output used in join tokens.
pub fn ca_fingerprint_from_pem(ca_pem: &[u8]) -> Result<String, String> {
    use sha2::{Digest, Sha256};

    // Extract DER bytes from PEM
    let pem_str =
        std::str::from_utf8(ca_pem).map_err(|e| format!("CA PEM is not valid UTF-8: {e}"))?;
    let pem = pem::parse(pem_str).map_err(|e| format!("Failed to parse PEM: {e}"))?;
    let der = pem.contents();

    // SHA-256 hash of DER-encoded certificate
    let hash = Sha256::digest(der);

    // Base64-encode without padding (matching Python's rstrip("="))
    use base64::engine::general_purpose::STANDARD_NO_PAD;
    use base64::Engine;
    let b64 = STANDARD_NO_PAD.encode(hash);

    Ok(format!("SHA256:{}", b64))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn generate_test_ca() -> (String, String) {
        let ca_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).unwrap();
        let mut params = CertificateParams::default();
        let mut dn = DistinguishedName::new();
        dn.push(DnType::OrganizationName, "Nexus");
        dn.push(DnType::CommonName, "nexus-zone-root-ca");
        params.distinguished_name = dn;
        params.is_ca = IsCa::Ca(rcgen::BasicConstraints::Constrained(0));
        params.key_usages = vec![
            KeyUsagePurpose::DigitalSignature,
            KeyUsagePurpose::KeyCertSign,
            KeyUsagePurpose::CrlSign,
        ];
        let ca_cert = params.self_signed(&ca_key).unwrap();
        (ca_cert.pem(), ca_key.serialize_pem())
    }

    #[test]
    fn test_generate_node_cert() {
        let (ca_cert_pem, ca_key_pem) = generate_test_ca();
        let (cert_pem, key_pem) = generate_node_cert(
            2,
            "root",
            ca_cert_pem.as_bytes(),
            ca_key_pem.as_bytes(),
            &[],
            Some("nexus-2"),
        )
        .unwrap();

        assert!(!cert_pem.is_empty());
        assert!(!key_pem.is_empty());
        assert!(String::from_utf8_lossy(&cert_pem).contains("BEGIN CERTIFICATE"));
        assert!(String::from_utf8_lossy(&key_pem).contains("BEGIN PRIVATE KEY"));
    }

    #[test]
    fn test_invalid_ca_key() {
        let (ca_cert_pem, _) = generate_test_ca();
        let result = generate_node_cert(1, "root", ca_cert_pem.as_bytes(), b"not-a-key", &[], None);
        assert!(result.is_err());
    }
}
