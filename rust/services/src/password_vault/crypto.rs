//! AES-256-GCM seal/open + master key handling for password_vault.
//!
//! Master key is 32 random bytes. Loaded from a file path on first
//! call; if the file doesn't exist, generated via `OsRng` and persisted
//! atomically (tmp file + rename). On Unix the file is chmod 0600
//! after write; on Windows we rely on default user-profile ACLs
//! (operators can tighten further with `icacls` if needed — see
//! README §Cross-box for the laptop bring-up procedure).
//!
//! Each `seal` generates a fresh 12-byte nonce via `OsRng`. The
//! 16-byte GCM auth tag is appended to the ciphertext per the
//! `aes-gcm` crate convention. Same security level as Python Fernet
//! (AES-256-GCM is the AEAD analogue of Fernet's AES-128-CBC+HMAC).

use std::path::Path;

use aes_gcm::aead::{Aead, AeadCore, KeyInit, OsRng};
use aes_gcm::{Aes256Gcm, Key, Nonce};

use super::types::PasswordVaultError;

/// 32-byte master key. `Debug` is redacted so tracing / panic
/// messages never leak it.
pub(crate) struct MasterKey([u8; 32]);

impl MasterKey {
    pub(crate) fn from_bytes(bytes: [u8; 32]) -> Self {
        Self(bytes)
    }

    pub(crate) fn generate() -> Self {
        let key = Aes256Gcm::generate_key(&mut OsRng);
        let mut bytes = [0u8; 32];
        bytes.copy_from_slice(key.as_slice());
        Self(bytes)
    }

    fn as_aes_key(&self) -> &Key<Aes256Gcm> {
        Key::<Aes256Gcm>::from_slice(&self.0)
    }
}

impl std::fmt::Debug for MasterKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("MasterKey(<redacted 32 bytes>)")
    }
}

/// Load master key from `path`, or generate + persist a fresh one
/// if the file doesn't exist. Atomic write (tmp + rename) avoids
/// half-written keys on crash.
pub(crate) fn load_or_create_master_key(path: &Path) -> Result<MasterKey, PasswordVaultError> {
    if path.exists() {
        let bytes = std::fs::read(path).map_err(|e| {
            PasswordVaultError::Storage(format!("read master key {}: {e}", path.display()))
        })?;
        if bytes.len() != 32 {
            return Err(PasswordVaultError::Storage(format!(
                "master key {} must be exactly 32 bytes, got {}",
                path.display(),
                bytes.len()
            )));
        }
        let mut arr = [0u8; 32];
        arr.copy_from_slice(&bytes);
        return Ok(MasterKey::from_bytes(arr));
    }

    // Generate + persist atomically.
    let key = MasterKey::generate();
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent).map_err(|e| {
                PasswordVaultError::Storage(format!(
                    "mkdir parent of {}: {e}",
                    path.display()
                ))
            })?;
        }
    }
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, key.0).map_err(|e| {
        PasswordVaultError::Storage(format!("write master key tmp {}: {e}", tmp.display()))
    })?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))
            .map_err(|e| {
                PasswordVaultError::Storage(format!("chmod 0600 on master key: {e}"))
            })?;
    }

    std::fs::rename(&tmp, path).map_err(|e| {
        PasswordVaultError::Storage(format!("atomic rename to {}: {e}", path.display()))
    })?;

    Ok(key)
}

/// Encrypt `plaintext` with `key`. Returns `(nonce, ciphertext_with_tag)`.
pub(crate) fn seal(
    plaintext: &[u8],
    key: &MasterKey,
) -> Result<([u8; 12], Vec<u8>), PasswordVaultError> {
    let cipher = Aes256Gcm::new(key.as_aes_key());
    let nonce_obj = Aes256Gcm::generate_nonce(&mut OsRng);
    let ciphertext = cipher
        .encrypt(&nonce_obj, plaintext)
        .map_err(|_| PasswordVaultError::Crypto)?;
    let mut nonce_bytes = [0u8; 12];
    nonce_bytes.copy_from_slice(nonce_obj.as_slice());
    Ok((nonce_bytes, ciphertext))
}

/// Decrypt `ciphertext` (with appended GCM tag) using `nonce` and
/// `key`. Returns the plaintext, or `Crypto` error on tag mismatch
/// (wrong key, tampered ciphertext, wrong nonce — all
/// indistinguishable, by design).
pub(crate) fn open(
    nonce: &[u8; 12],
    ciphertext: &[u8],
    key: &MasterKey,
) -> Result<Vec<u8>, PasswordVaultError> {
    let cipher = Aes256Gcm::new(key.as_aes_key());
    // `Nonce` default size is U12 — matches AES-GCM's 96-bit nonce.
    // Passing `Nonce::<Aes256Gcm>` is a type error: Nonce's parameter
    // is a typenum size, not a cipher.
    let nonce_obj = Nonce::from_slice(nonce);
    cipher
        .decrypt(nonce_obj, ciphertext)
        .map_err(|_| PasswordVaultError::Crypto)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn round_trip() {
        let key = MasterKey::generate();
        let plain = b"hello vault";
        let (nonce, ct) = seal(plain, &key).unwrap();
        let back = open(&nonce, &ct, &key).unwrap();
        assert_eq!(back, plain);
    }

    #[test]
    fn wrong_key_fails() {
        let key1 = MasterKey::generate();
        let key2 = MasterKey::generate();
        let (nonce, ct) = seal(b"secret", &key1).unwrap();
        assert!(open(&nonce, &ct, &key2).is_err());
    }

    #[test]
    fn tampered_ciphertext_fails() {
        let key = MasterKey::generate();
        let (nonce, mut ct) = seal(b"secret", &key).unwrap();
        ct[0] ^= 1; // flip a bit in the body
        assert!(open(&nonce, &ct, &key).is_err());
    }

    #[test]
    fn empty_plaintext_round_trips() {
        // Edge case: zero-length entries should still encrypt cleanly
        // (e.g. a title with no fields filled in).
        let key = MasterKey::generate();
        let (nonce, ct) = seal(b"", &key).unwrap();
        assert_eq!(open(&nonce, &ct, &key).unwrap(), b"");
    }

    #[test]
    fn load_creates_then_loads() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("master.key");
        // First call: file doesn't exist → generate + persist.
        let k1 = load_or_create_master_key(&path).unwrap();
        assert!(path.exists());
        // Second call: file exists → load identical bytes.
        let k2 = load_or_create_master_key(&path).unwrap();
        assert_eq!(k1.0, k2.0);
    }

    #[test]
    fn load_rejects_wrong_length() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("bad.key");
        std::fs::write(&path, b"only 8 bytes").unwrap();
        assert!(load_or_create_master_key(&path).is_err());
    }
}
