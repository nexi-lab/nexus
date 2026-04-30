//! Binary format constants and header for the trigram index.
//!
//! Layout:
//! ```text
//! ┌─────────────────────────────────────────────┐
//! │ Header (48 bytes)                           │
//! │  magic: [u8; 4] = "TRGM"                   │
//! │  version: u32 = 1                           │
//! │  flags: u32                                 │
//! │  file_count: u32                            │
//! │  trigram_count: u32                         │
//! │  file_table_offset: u64                     │
//! │  trigram_table_offset: u64                  │
//! │  posting_offset: u64                        │
//! │  header_crc32: u32                          │
//! ├─────────────────────────────────────────────┤
//! │ File Table                                  │
//! │  entries + path bytes + section_crc32       │
//! ├─────────────────────────────────────────────┤
//! │ Trigram Table                               │
//! │  sorted trigram entries + section_crc32     │
//! ├─────────────────────────────────────────────┤
//! │ Posting Lists                               │
//! │  Roaring bitmap serialized + section_crc32  │
//! └─────────────────────────────────────────────┘
//! ```

/// Magic bytes identifying a trigram index file.
pub const MAGIC: [u8; 4] = *b"TRGM";

/// Current format version.
pub const VERSION: u32 = 1;

/// Header size in bytes (fixed).
pub const HEADER_SIZE: usize = 48;

/// File table entry: file_id (u32) + path_offset (u32) + path_len (u16) = 10 bytes.
pub const FILE_ENTRY_SIZE: usize = 10;

/// Trigram table entry: trigram (3 bytes) + posting_offset (u32) + posting_len (u32) = 11 bytes.
pub const TRIGRAM_ENTRY_SIZE: usize = 11;

/// Index header parsed from bytes.
#[derive(Debug, Clone)]
pub struct IndexHeader {
    pub version: u32,
    pub flags: u32,
    pub file_count: u32,
    pub trigram_count: u32,
    pub file_table_offset: u64,
    pub trigram_table_offset: u64,
    pub posting_offset: u64,
}

impl IndexHeader {
    /// Serialize header to bytes (48 bytes, little-endian).
    /// CRC32 is computed over the first 44 bytes and appended as bytes 44-47.
    pub fn to_bytes(&self) -> [u8; HEADER_SIZE] {
        let mut buf = [0u8; HEADER_SIZE];
        buf[0..4].copy_from_slice(&MAGIC);
        buf[4..8].copy_from_slice(&self.version.to_le_bytes());
        buf[8..12].copy_from_slice(&self.flags.to_le_bytes());
        buf[12..16].copy_from_slice(&self.file_count.to_le_bytes());
        buf[16..20].copy_from_slice(&self.trigram_count.to_le_bytes());
        buf[20..28].copy_from_slice(&self.file_table_offset.to_le_bytes());
        buf[28..36].copy_from_slice(&self.trigram_table_offset.to_le_bytes());
        buf[36..44].copy_from_slice(&self.posting_offset.to_le_bytes());
        // CRC32 over first 44 bytes.
        let crc = crc32fast::hash(&buf[..44]);
        buf[44..48].copy_from_slice(&crc.to_le_bytes());
        buf
    }

    /// Parse header from bytes. Returns None if invalid.
    pub fn from_bytes(data: &[u8]) -> Option<Self> {
        if data.len() < HEADER_SIZE {
            return None;
        }

        // Check magic.
        if data[0..4] != MAGIC {
            return None;
        }

        // Check CRC32.
        let stored_crc = u32::from_le_bytes([data[44], data[45], data[46], data[47]]);
        let computed_crc = crc32fast::hash(&data[..44]);
        if stored_crc != computed_crc {
            return None;
        }

        Some(IndexHeader {
            version: u32::from_le_bytes([data[4], data[5], data[6], data[7]]),
            flags: u32::from_le_bytes([data[8], data[9], data[10], data[11]]),
            file_count: u32::from_le_bytes([data[12], data[13], data[14], data[15]]),
            trigram_count: u32::from_le_bytes([data[16], data[17], data[18], data[19]]),
            file_table_offset: u64::from_le_bytes([
                data[20], data[21], data[22], data[23], data[24], data[25], data[26], data[27],
            ]),
            trigram_table_offset: u64::from_le_bytes([
                data[28], data[29], data[30], data[31], data[32], data[33], data[34], data[35],
            ]),
            posting_offset: u64::from_le_bytes([
                data[36], data[37], data[38], data[39], data[40], data[41], data[42], data[43],
            ]),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_header_roundtrip() {
        let header = IndexHeader {
            version: VERSION,
            flags: 0,
            file_count: 100,
            trigram_count: 5000,
            file_table_offset: 48,
            trigram_table_offset: 1234,
            posting_offset: 5678,
        };

        let bytes = header.to_bytes();
        assert_eq!(bytes.len(), HEADER_SIZE);
        assert_eq!(&bytes[0..4], &MAGIC);

        let parsed = IndexHeader::from_bytes(&bytes).expect("Should parse valid header");
        assert_eq!(parsed.version, VERSION);
        assert_eq!(parsed.file_count, 100);
        assert_eq!(parsed.trigram_count, 5000);
        assert_eq!(parsed.file_table_offset, 48);
        assert_eq!(parsed.trigram_table_offset, 1234);
        assert_eq!(parsed.posting_offset, 5678);
    }

    #[test]
    fn test_header_invalid_magic() {
        let mut bytes = IndexHeader {
            version: VERSION,
            flags: 0,
            file_count: 0,
            trigram_count: 0,
            file_table_offset: 0,
            trigram_table_offset: 0,
            posting_offset: 0,
        }
        .to_bytes();

        bytes[0] = b'X'; // Corrupt magic.
        assert!(IndexHeader::from_bytes(&bytes).is_none());
    }

    #[test]
    fn test_header_corrupt_crc() {
        let mut bytes = IndexHeader {
            version: VERSION,
            flags: 0,
            file_count: 42,
            trigram_count: 0,
            file_table_offset: 0,
            trigram_table_offset: 0,
            posting_offset: 0,
        }
        .to_bytes();

        bytes[44] ^= 0xFF; // Corrupt CRC.
        assert!(IndexHeader::from_bytes(&bytes).is_none());
    }

    #[test]
    fn test_header_too_short() {
        assert!(IndexHeader::from_bytes(&[0u8; 10]).is_none());
    }
}
