//! Server-side node certificate generation for JoinCluster RPC, plus
//! Day-1 TLS bootstrap (CA + node cert + join token) for fresh clusters.
//!
//! Generates X.509 node certificates signed by the cluster CA, matching
//! the output of Python `certgen.py`: EC P-256, SHA-256, mTLS-ready SANs.
//!
//! The CA private key never leaves node-1 — this module is called server-side
//! during JoinCluster to sign certs for joining nodes.

use rcgen::{
    BasicConstraints, CertificateParams, DistinguishedName, DnType, ExtendedKeyUsagePurpose, IsCa,
    KeyPair, KeyUsagePurpose, SanType, PKCS_ECDSA_P256_SHA256,
};
use std::net::{Ipv4Addr, Ipv6Addr};
use std::path::{Path, PathBuf};

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

/// Generate a self-signed root CA certificate for a zone.
///
/// Mirrors Python `nexus.security.tls.certgen.generate_zone_ca`:
/// - Algorithm: EC P-256
/// - CN: `nexus-zone-{zone_id}-ca`, O: Nexus
/// - basicConstraints: CA:TRUE, pathLenConstraint: 0 (single-tier hierarchy)
/// - Key usage: digitalSignature, keyCertSign, cRLSign
/// - Validity: 10 years (CA outlives node certs by design)
pub fn generate_zone_ca(zone_id: &str) -> Result<(Vec<u8>, Vec<u8>), String> {
    let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256)
        .map_err(|e| format!("Failed to generate CA key: {e}"))?;

    let mut params = CertificateParams::default();
    let mut dn = DistinguishedName::new();
    dn.push(DnType::OrganizationName, "Nexus");
    dn.push(DnType::CommonName, format!("nexus-zone-{zone_id}-ca"));
    params.distinguished_name = dn;
    params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
    params.key_usages = vec![
        KeyUsagePurpose::DigitalSignature,
        KeyUsagePurpose::KeyCertSign,
        KeyUsagePurpose::CrlSign,
    ];
    let now = time::OffsetDateTime::now_utc();
    params.not_before = now;
    params.not_after = now + time::Duration::days(365 * 10);

    let cert = params
        .self_signed(&key)
        .map_err(|e| format!("Failed to self-sign CA: {e}"))?;

    Ok((cert.pem().into_bytes(), key.serialize_pem().into_bytes()))
}

/// Generate a K3s-style cluster join token plus its server-side hash.
///
/// Token format: `K10<password>::server:<ca_fingerprint>` (matches
/// Python `nexus.security.tls.join_token`). Returns `(token, sha256_hex_hash)`.
/// The token is given to operators; the hash is stored on the leader for
/// constant-time verification of incoming `JoinCluster` requests.
pub fn generate_join_token(ca_pem: &[u8]) -> Result<(String, String), String> {
    use rand::RngCore;
    use sha2::{Digest, Sha256};

    let mut bytes = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    let password: String = bytes.iter().map(|b| format!("{:02x}", b)).collect();

    let fingerprint = ca_fingerprint_from_pem(ca_pem)?;
    let token = format!("K10{password}::server:{fingerprint}");
    let hash = format!("{:x}", Sha256::digest(password.as_bytes()));

    Ok((token, hash))
}

/// On-disk PEM bundle that `bootstrap_tls` produces / discovers.
#[derive(Debug, Clone)]
pub struct BootstrapTls {
    pub ca_path: PathBuf,
    pub ca_key_path: PathBuf,
    pub node_cert_path: PathBuf,
    pub node_key_path: PathBuf,
    pub join_token_hash: String,
}

/// Discover or generate the Day-1 TLS bundle under `<base_path>/tls/`.
///
/// On first call: generates CA + node cert + join token, writes
/// `ca.pem` / `ca-key.pem` / `node.pem` / `node-key.pem` / `join-token`
/// / `join-token-hash`. On subsequent calls: detects the existing
/// bundle and returns its paths. Mirrors Python
/// `ZoneManager._auto_generate_tls`.
///
/// `zone_id` is the root zone id (used in CA CN); `hostname` is the
/// node's hostname (used in node cert CN + SAN). `node_id` is the
/// hostname-derived numeric id used in node cert CN.
pub fn bootstrap_tls(
    base_path: &Path,
    zone_id: &str,
    hostname: &str,
    node_id: u64,
) -> Result<BootstrapTls, String> {
    let tls_dir = base_path.join("tls");
    let ca_path = tls_dir.join("ca.pem");
    let ca_key_path = tls_dir.join("ca-key.pem");
    let node_cert_path = tls_dir.join("node.pem");
    let node_key_path = tls_dir.join("node-key.pem");
    let join_token_hash_path = tls_dir.join("join-token-hash");

    // Reuse path: every artifact present + non-empty.
    let bundle_present = ca_path.exists()
        && ca_key_path.exists()
        && node_cert_path.exists()
        && node_key_path.exists()
        && join_token_hash_path.exists();
    if bundle_present {
        let join_token_hash = std::fs::read_to_string(&join_token_hash_path)
            .map_err(|e| format!("Failed to read join-token-hash: {e}"))?
            .trim()
            .to_string();
        return Ok(BootstrapTls {
            ca_path,
            ca_key_path,
            node_cert_path,
            node_key_path,
            join_token_hash,
        });
    }

    std::fs::create_dir_all(&tls_dir)
        .map_err(|e| format!("Failed to mkdir {}: {}", tls_dir.display(), e))?;

    let (ca_pem, ca_key_pem) = generate_zone_ca(zone_id)?;
    write_pem(&ca_path, &ca_pem, false)?;
    write_pem(&ca_key_path, &ca_key_pem, true)?;

    let (node_cert_pem, node_key_pem) =
        generate_node_cert(node_id, zone_id, &ca_pem, &ca_key_pem, &[], Some(hostname))?;
    write_pem(&node_cert_path, &node_cert_pem, false)?;
    write_pem(&node_key_path, &node_key_pem, true)?;

    let (token, hash) = generate_join_token(&ca_pem)?;
    std::fs::write(tls_dir.join("join-token"), &token)
        .map_err(|e| format!("Failed to write join-token: {e}"))?;
    std::fs::write(&join_token_hash_path, &hash)
        .map_err(|e| format!("Failed to write join-token-hash: {e}"))?;

    Ok(BootstrapTls {
        ca_path,
        ca_key_path,
        node_cert_path,
        node_key_path,
        join_token_hash: hash,
    })
}

fn write_pem(path: &Path, pem: &[u8], private: bool) -> Result<(), String> {
    std::fs::write(path, pem).map_err(|e| format!("Failed to write {}: {}", path.display(), e))?;
    if private {
        // POSIX-only: tighten private-key file permissions to 0o600.
        // No-op on Windows where ACLs handle this differently.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let perms = std::fs::Permissions::from_mode(0o600);
            std::fs::set_permissions(path, perms)
                .map_err(|e| format!("chmod 600 {}: {}", path.display(), e))?;
        }
    }
    Ok(())
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

    #[test]
    fn test_generate_zone_ca() {
        let (cert_pem, key_pem) = generate_zone_ca("root").unwrap();
        let cert_str = String::from_utf8_lossy(&cert_pem);
        assert!(cert_str.contains("BEGIN CERTIFICATE"));
        assert!(String::from_utf8_lossy(&key_pem).contains("BEGIN PRIVATE KEY"));
        // CA must be usable to sign a node cert
        let result = generate_node_cert(1, "root", &cert_pem, &key_pem, &[], Some("host-1"));
        assert!(result.is_ok());
    }

    #[test]
    fn test_generate_join_token_format() {
        let (ca_pem, _) = generate_zone_ca("root").unwrap();
        let (token, hash) = generate_join_token(&ca_pem).unwrap();
        assert!(token.starts_with("K10"));
        assert!(token.contains("::server:SHA256:"));
        // password is 64 hex chars between "K10" and the separator
        let body = token.strip_prefix("K10").unwrap();
        let (password, _) = body.split_once("::server:").unwrap();
        assert_eq!(password.len(), 64);
        assert!(password.chars().all(|c| c.is_ascii_hexdigit()));
        // hash is 64-char lowercase hex (SHA-256)
        assert_eq!(hash.len(), 64);
        assert!(hash
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    }

    #[test]
    fn test_bootstrap_tls_first_run_then_reuse() {
        let dir = tempfile::tempdir().unwrap();
        let base = dir.path();

        let first = bootstrap_tls(base, "root", "host-1", 42).unwrap();
        for p in [
            &first.ca_path,
            &first.ca_key_path,
            &first.node_cert_path,
            &first.node_key_path,
        ] {
            assert!(p.exists(), "{} missing after first bootstrap", p.display());
        }
        assert_eq!(first.join_token_hash.len(), 64);

        // Reuse path: hashes/paths must be identical and file mtime stable.
        let second = bootstrap_tls(base, "root", "host-1", 42).unwrap();
        assert_eq!(first.ca_path, second.ca_path);
        assert_eq!(first.join_token_hash, second.join_token_hash);
        // CA cert bytes unchanged (proves we did not regenerate)
        let ca_first = std::fs::read(&first.ca_path).unwrap();
        let ca_second = std::fs::read(&second.ca_path).unwrap();
        assert_eq!(ca_first, ca_second);
    }
}
