//! Posting list operations using Roaring bitmaps.

use roaring::RoaringBitmap;

/// A posting list wrapping a Roaring bitmap of file IDs.
#[derive(Debug, Clone)]
pub struct PostingList {
    pub bitmap: RoaringBitmap,
}

impl PostingList {
    /// Create an empty posting list.
    pub fn new() -> Self {
        PostingList {
            bitmap: RoaringBitmap::new(),
        }
    }

    /// Create a posting list from a Roaring bitmap.
    pub fn from_bitmap(bitmap: RoaringBitmap) -> Self {
        PostingList { bitmap }
    }

    /// Insert a file ID.
    pub fn insert(&mut self, file_id: u32) {
        self.bitmap.insert(file_id);
    }

    /// Number of file IDs in this posting list.
    pub fn len(&self) -> u64 {
        self.bitmap.len()
    }

    /// Returns true if empty.
    pub fn is_empty(&self) -> bool {
        self.bitmap.is_empty()
    }

    /// Iterate over file IDs.
    pub fn iter(&self) -> roaring::bitmap::Iter<'_> {
        self.bitmap.iter()
    }

    /// Convert to a Vec of file IDs.
    pub fn to_vec(&self) -> Vec<u32> {
        self.bitmap.iter().collect()
    }
}

impl Default for PostingList {
    fn default() -> Self {
        Self::new()
    }
}

/// Intersect multiple posting lists (AND operation).
///
/// Returns an empty posting list if any input is empty.
/// Returns the first list if only one is provided.
pub fn intersect(lists: &[PostingList]) -> PostingList {
    if lists.is_empty() {
        return PostingList::new();
    }

    let mut result = lists[0].bitmap.clone();
    for list in &lists[1..] {
        result &= &list.bitmap;
        // Early exit if intersection is empty.
        if result.is_empty() {
            break;
        }
    }

    PostingList::from_bitmap(result)
}

/// Union multiple posting lists (OR operation).
///
/// Returns an empty posting list if no inputs.
pub fn union(lists: &[PostingList]) -> PostingList {
    if lists.is_empty() {
        return PostingList::new();
    }

    let mut result = lists[0].bitmap.clone();
    for list in &lists[1..] {
        result |= &list.bitmap;
    }

    PostingList::from_bitmap(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_intersect_two_lists() {
        let mut a = PostingList::new();
        a.insert(1);
        a.insert(2);
        a.insert(3);

        let mut b = PostingList::new();
        b.insert(2);
        b.insert(3);
        b.insert(4);

        let result = intersect(&[a, b]);
        assert_eq!(result.to_vec(), vec![2, 3]);
    }

    #[test]
    fn test_intersect_with_empty() {
        let mut a = PostingList::new();
        a.insert(1);
        a.insert(2);

        let b = PostingList::new();

        let result = intersect(&[a, b]);
        assert!(result.is_empty());
    }

    #[test]
    fn test_intersect_single_element() {
        let mut a = PostingList::new();
        a.insert(42);

        let mut b = PostingList::new();
        b.insert(42);
        b.insert(99);

        let result = intersect(&[a, b]);
        assert_eq!(result.to_vec(), vec![42]);
    }

    #[test]
    fn test_intersect_disjoint() {
        let mut a = PostingList::new();
        a.insert(1);
        a.insert(2);

        let mut b = PostingList::new();
        b.insert(3);
        b.insert(4);

        let result = intersect(&[a, b]);
        assert!(result.is_empty());
    }

    #[test]
    fn test_union_posting_lists() {
        let mut a = PostingList::new();
        a.insert(1);
        a.insert(2);

        let mut b = PostingList::new();
        b.insert(2);
        b.insert(3);

        let result = union(&[a, b]);
        assert_eq!(result.to_vec(), vec![1, 2, 3]);
    }

    #[test]
    fn test_intersect_empty_input() {
        let result = intersect(&[]);
        assert!(result.is_empty());
    }

    #[test]
    fn test_union_empty_input() {
        let result = union(&[]);
        assert!(result.is_empty());
    }

    #[test]
    fn test_posting_list_len() {
        let mut pl = PostingList::new();
        assert_eq!(pl.len(), 0);
        pl.insert(1);
        pl.insert(2);
        assert_eq!(pl.len(), 2);
    }
}
