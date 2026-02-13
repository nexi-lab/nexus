//! Glob pattern matching using the `globset` crate.

use globset::{Glob, GlobSet, GlobSetBuilder};

/// Build a `GlobSet` from a list of glob patterns.
pub fn build_globset(patterns: &[String]) -> Result<GlobSet, globset::Error> {
    let mut builder = GlobSetBuilder::new();
    for pattern in patterns {
        builder.add(Glob::new(pattern)?);
    }
    builder.build()
}

/// Filter paths by glob patterns — return paths that match any pattern.
pub fn glob_match(patterns: &[String], paths: &[String]) -> Result<Vec<String>, globset::Error> {
    let globset = build_globset(patterns)?;
    Ok(paths
        .iter()
        .filter(|path| globset.is_match(path.as_str()))
        .cloned()
        .collect())
}

/// Filter paths by exclude patterns — return paths that do NOT match.
pub fn filter_paths_exclude(
    paths: &[String],
    exclude_patterns: &[String],
) -> Result<Vec<String>, globset::Error> {
    let globset = build_globset(exclude_patterns)?;
    Ok(paths
        .iter()
        .filter(|path| {
            let filename = path.rsplit('/').next().unwrap_or(path);
            !globset.is_match(filename)
        })
        .cloned()
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn basic_glob_match() {
        let patterns = vec!["*.rs".to_string()];
        let paths = vec![
            "main.rs".to_string(),
            "lib.rs".to_string(),
            "readme.md".to_string(),
        ];
        let matched = glob_match(&patterns, &paths).unwrap();
        assert_eq!(matched, vec!["main.rs", "lib.rs"]);
    }

    #[test]
    fn exclude_filter() {
        let paths = vec![
            "src/main.rs".to_string(),
            "src/.hidden".to_string(),
            "target/debug/build".to_string(),
        ];
        let exclude = vec![".*".to_string()];
        let filtered = filter_paths_exclude(&paths, &exclude).unwrap();
        assert_eq!(filtered, vec!["src/main.rs", "target/debug/build"]);
    }

    #[test]
    fn empty_patterns_match_nothing() {
        let paths = vec!["test.rs".to_string()];
        let matched = glob_match(&[], &paths).unwrap();
        assert!(matched.is_empty());
    }
}
