//! Criterion benchmarks for path prefix matching (Issue #1565).
//!
//! Run: cd rust/kernel && cargo bench prefix

use criterion::{black_box, criterion_group, criterion_main, BenchmarkId, Criterion};

// Import the prefix functions from the lib crate.
// Since nexus_kernel is a cdylib, we access the module directly.
mod prefix {
    /// Normalize a prefix: strip trailing slashes.
    #[inline]
    pub fn normalize_prefix(prefix: &str) -> &str {
        let trimmed = prefix.trim_end_matches('/');
        if trimmed.is_empty() {
            return "";
        }
        trimmed
    }

    #[inline]
    pub fn path_matches_prefix(path: &str, prefix_norm: &str) -> bool {
        if prefix_norm.is_empty() {
            return true;
        }
        if let Some(rest) = path.strip_prefix(prefix_norm) {
            rest.is_empty() || rest.starts_with('/')
        } else {
            false
        }
    }

    pub fn any_path_starts_with(mut paths: Vec<String>, prefix: &str) -> bool {
        if paths.is_empty() {
            return false;
        }
        let prefix_norm = normalize_prefix(prefix);
        if prefix_norm.is_empty() {
            return true;
        }
        paths.sort_unstable();
        let idx = paths.partition_point(|p| p.as_str() < prefix_norm);
        for path in paths[idx..].iter() {
            if path_matches_prefix(path, prefix_norm) {
                return true;
            }
            if !path.starts_with(prefix_norm) {
                break;
            }
        }
        false
    }

    pub fn batch_prefix_check(mut paths: Vec<String>, prefixes: Vec<String>) -> Vec<bool> {
        if paths.is_empty() {
            return vec![false; prefixes.len()];
        }
        paths.sort_unstable();
        prefixes
            .iter()
            .map(|prefix| {
                let prefix_norm = normalize_prefix(prefix);
                if prefix_norm.is_empty() {
                    return true;
                }
                let idx = paths.partition_point(|p| p.as_str() < prefix_norm);
                for path in paths[idx..].iter() {
                    if path_matches_prefix(path, prefix_norm) {
                        return true;
                    }
                    if !path.starts_with(prefix_norm) {
                        break;
                    }
                }
                false
            })
            .collect()
    }

    /// Naive linear scan for comparison
    pub fn batch_prefix_check_naive(paths: &[String], prefixes: &[String]) -> Vec<bool> {
        prefixes
            .iter()
            .map(|prefix| {
                let prefix_norm = prefix.trim_end_matches('/');
                let prefix_slash = format!("{}/", prefix_norm);
                paths
                    .iter()
                    .any(|p| p.starts_with(&prefix_slash) || p == prefix_norm)
            })
            .collect()
    }
}

fn generate_paths(n: usize) -> Vec<String> {
    (0..n)
        .map(|i| {
            format!(
                "/workspace/project-{}/src/module-{}/file-{}.rs",
                i % 50,
                i % 200,
                i
            )
        })
        .collect()
}

fn generate_prefixes(m: usize) -> Vec<String> {
    (0..m)
        .map(|i| format!("/workspace/project-{}", i))
        .collect()
}

fn bench_any_path_starts_with(c: &mut Criterion) {
    let mut group = c.benchmark_group("any_path_starts_with");

    for size in [100, 1_000, 10_000, 100_000] {
        let paths = generate_paths(size);
        group.bench_with_input(BenchmarkId::from_parameter(size), &paths, |b, paths| {
            b.iter(|| {
                prefix::any_path_starts_with(black_box(paths.clone()), "/workspace/project-25")
            })
        });
    }
    group.finish();
}

fn bench_batch_prefix_check(c: &mut Criterion) {
    let mut group = c.benchmark_group("batch_prefix_check");

    for (n_paths, n_prefixes) in [(1_000, 10), (10_000, 50), (100_000, 100)] {
        let paths = generate_paths(n_paths);
        let prefixes = generate_prefixes(n_prefixes);
        let label = format!("{}p_{}x", n_paths, n_prefixes);

        group.bench_with_input(
            BenchmarkId::new("sorted_bisect", &label),
            &(&paths, &prefixes),
            |b, (paths, prefixes)| {
                b.iter(|| {
                    prefix::batch_prefix_check(
                        black_box((*paths).clone()),
                        black_box((*prefixes).clone()),
                    )
                })
            },
        );

        group.bench_with_input(
            BenchmarkId::new("naive_linear", &label),
            &(&paths, &prefixes),
            |b, (paths, prefixes)| {
                b.iter(|| prefix::batch_prefix_check_naive(black_box(*paths), black_box(*prefixes)))
            },
        );
    }
    group.finish();
}

criterion_group!(
    benches,
    bench_any_path_starts_with,
    bench_batch_prefix_check
);
criterion_main!(benches);
