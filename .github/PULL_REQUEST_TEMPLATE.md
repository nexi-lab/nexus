## Summary
<!-- 1-3 bullet points -->

## Test plan
<!-- How was this tested? -->

---

> **Kernel Review Gate**: PRs touching `rust/`, `Cargo.toml`, `Cargo.lock`,
> `scripts/codegen_kernel_abi.py`, or `stubs/` require **kernel team (@elfenlieds7) approval**
> before merge (CODEOWNERS enforced). Do NOT add new crates or dependencies
> to `rust/kernel/` without kernel team design review. Do NOT bypass syscalls
> by calling ObjectStore/Metastore directly. Read `docs/architecture/KERNEL-ARCHITECTURE.md`.
