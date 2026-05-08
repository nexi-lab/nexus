use criterion::{black_box, criterion_group, criterion_main, Criterion};

#[allow(dead_code)]
#[path = "../src/core/dispatch/ops_registry.rs"]
mod ops_registry;

use ops_registry::{BackendKind, CatHandlerKind, FileType, OpHandler, OpKey, OpName, OpsRegistry};

fn direct_default() -> OpHandler {
    OpHandler::Cat(CatHandlerKind::Default)
}

fn registry_default(registry: &OpsRegistry) -> Option<OpHandler> {
    registry.resolve("cat", &FileType::Unknown, &BackendKind::Local)
}

fn bench_ops_registry(c: &mut Criterion) {
    let registry = OpsRegistry::new();
    registry
        .register(
            OpKey::new(OpName::new("cat"), None, None),
            OpHandler::Cat(CatHandlerKind::Default),
        )
        .unwrap();

    c.bench_function("ops_direct_default", |b| {
        b.iter(|| black_box(direct_default()))
    });
    c.bench_function("ops_registry_default", |b| {
        b.iter(|| black_box(registry_default(black_box(&registry))))
    });
}

criterion_group!(benches, bench_ops_registry);
criterion_main!(benches);
