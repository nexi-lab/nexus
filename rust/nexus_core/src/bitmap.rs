//! Roaring Bitmap operations for Tiger Cache acceleration.

use roaring::RoaringBitmap;

/// Filter path IDs using a deserialized Roaring Bitmap.
///
/// Returns IDs present in both the input list and the bitmap.
pub fn filter_with_bitmap(path_int_ids: &[u32], bitmap: &RoaringBitmap) -> Vec<u32> {
    path_int_ids
        .iter()
        .filter(|&&id| bitmap.contains(id))
        .copied()
        .collect()
}

/// Intersect path IDs with a bitmap using native bitmap intersection.
///
/// More efficient than `filter_with_bitmap` when the bitmap is smaller.
pub fn intersect_with_bitmap(path_int_ids: &[u32], bitmap: &RoaringBitmap) -> Vec<u32> {
    let input_bitmap: RoaringBitmap = path_int_ids.iter().copied().collect();
    let result = input_bitmap & bitmap.clone();
    result.iter().collect()
}

/// Check if any path IDs are present in the bitmap. Early-exit on first match.
pub fn any_accessible(path_int_ids: &[u32], bitmap: &RoaringBitmap) -> bool {
    path_int_ids.iter().any(|&id| bitmap.contains(id))
}

/// Deserialize a Roaring Bitmap from bytes (standard RoaringFormatSpec).
pub fn deserialize_bitmap(bytes: &[u8]) -> Result<RoaringBitmap, std::io::Error> {
    RoaringBitmap::deserialize_from(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_bitmap(ids: &[u32]) -> RoaringBitmap {
        ids.iter().copied().collect()
    }

    #[test]
    fn filter_basic() {
        let bitmap = make_bitmap(&[1, 3, 5, 7, 9]);
        let input = vec![1, 2, 3, 4, 5];
        let result = filter_with_bitmap(&input, &bitmap);
        assert_eq!(result, vec![1, 3, 5]);
    }

    #[test]
    fn intersect_basic() {
        let bitmap = make_bitmap(&[1, 3, 5, 7, 9]);
        let input = vec![2, 3, 5, 8];
        let result = intersect_with_bitmap(&input, &bitmap);
        assert_eq!(result, vec![3, 5]);
    }

    #[test]
    fn any_accessible_true() {
        let bitmap = make_bitmap(&[10, 20, 30]);
        assert!(any_accessible(&[5, 10, 15], &bitmap));
    }

    #[test]
    fn any_accessible_false() {
        let bitmap = make_bitmap(&[10, 20, 30]);
        assert!(!any_accessible(&[1, 2, 3], &bitmap));
    }

    #[test]
    fn serialize_deserialize_roundtrip() {
        let bitmap = make_bitmap(&[1, 100, 1000, 10000]);
        let mut bytes = Vec::new();
        bitmap.serialize_into(&mut bytes).unwrap();
        let deserialized = deserialize_bitmap(&bytes).unwrap();
        assert_eq!(bitmap, deserialized);
    }

    #[test]
    fn empty_inputs() {
        let bitmap = make_bitmap(&[1, 2, 3]);
        assert!(filter_with_bitmap(&[], &bitmap).is_empty());
        assert!(!any_accessible(&[], &bitmap));

        let empty_bitmap = make_bitmap(&[]);
        assert!(filter_with_bitmap(&[1, 2], &empty_bitmap).is_empty());
    }
}
