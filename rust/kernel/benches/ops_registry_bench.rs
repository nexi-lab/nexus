use criterion::{black_box, criterion_group, criterion_main, Criterion};

#[allow(dead_code)]
#[path = "../src/core/dispatch/ops_registry.rs"]
mod ops_registry;

use ops_registry::{BackendKind, CatHandlerKind, FileType, OpHandler, OpKey, OpName, OpsRegistry};

fn direct_default(op: &str, filetype: &FileType, backend: &BackendKind) -> Option<OpHandler> {
    if !op.eq_ignore_ascii_case("cat") {
        return None;
    }
    match (filetype, backend) {
        (FileType::Json, BackendKind::GitHub) => Some(OpHandler::Cat(CatHandlerKind::GitHubJson)),
        (_, BackendKind::GitHub) => {
            Some(OpHandler::RawRead(ops_registry::RawReadHandlerKind::GitHub))
        }
        (FileType::Json, _) => Some(OpHandler::Cat(CatHandlerKind::JsonPretty)),
        _ => Some(OpHandler::Cat(CatHandlerKind::Default)),
    }
}

fn registry_default(registry: &OpsRegistry) -> Option<OpHandler> {
    registry.resolve("cat", &FileType::Unknown, &BackendKind::Local)
}

fn bench_ops_registry(c: &mut Criterion) {
    let mut registry = OpsRegistry::new();
    registry
        .register(
            OpKey::new(OpName::new("cat"), None, None),
            OpHandler::Cat(CatHandlerKind::Default),
        )
        .unwrap();
    registry
        .register(
            OpKey::new(OpName::new("cat"), Some(FileType::Json), None),
            OpHandler::Cat(CatHandlerKind::JsonPretty),
        )
        .unwrap();
    registry
        .register(
            OpKey::new(OpName::new("cat"), None, Some(BackendKind::GitHub)),
            OpHandler::RawRead(ops_registry::RawReadHandlerKind::GitHub),
        )
        .unwrap();
    registry
        .register(
            OpKey::new(
                OpName::new("cat"),
                Some(FileType::Json),
                Some(BackendKind::GitHub),
            ),
            OpHandler::Cat(CatHandlerKind::GitHubJson),
        )
        .unwrap();

    c.bench_function("ops_direct_default", |b| {
        b.iter(|| {
            black_box(direct_default(
                black_box("cat"),
                black_box(&FileType::Unknown),
                black_box(&BackendKind::Local),
            ))
        })
    });
    c.bench_function("ops_registry_default", |b| {
        b.iter(|| black_box(registry_default(black_box(&registry))))
    });
}

criterion_group!(benches, bench_ops_registry);
criterion_main!(benches);
