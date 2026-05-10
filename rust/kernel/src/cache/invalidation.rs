#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CacheInvalidation {
    Path { zone_id: String, path: String },
    ParentListing { zone_id: String, path: String },
}
