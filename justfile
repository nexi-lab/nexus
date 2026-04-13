# justfile — Nexus repo-root task runner
# Install just: https://github.com/casey/just
# Usage: just setup        # build all Rust crates
#        just doctor       # verify env is healthy
#        just build-kernel # rebuild only kernel

# Build every Rust crate as a Python extension (editable install).
# Uses `uv run` so maturin and python operate on the repo-managed venv,
# not whatever happens to be on ambient PATH.
# Run after: git clone, git pull, or switching branches with Rust changes.
setup:
    @echo "Building all Rust crates..."
    uv run maturin develop --release -m rust/kernel/Cargo.toml
    uv run maturin develop --release -m rust/raft/Cargo.toml
    uv run maturin develop --release -m rust/tasks/Cargo.toml
    @echo "Done. Run 'just doctor' to verify."

# Verify the environment is healthy (binary matches source ABI).
# Validates both MODULE_CAPABILITY_GROUPS (module-level symbols) and
# KERNEL_REQUIRED_METHODS (Kernel class methods) against the installed binary.
doctor:
    uv run python -c "
import sys, nexus_kernel
from nexus._kernel_api_groups import KERNEL_REQUIRED_METHODS, MODULE_CAPABILITY_GROUPS

print(f'nexus_kernel: {nexus_kernel.__file__}')
errors = []

# Check module-level capability groups
for group, symbols in MODULE_CAPABILITY_GROUPS.items():
    missing = [s for s in symbols if not hasattr(nexus_kernel, s)]
    if missing:
        errors.append(f'  group {group!r}: missing {missing}')

# Check Kernel class methods
kernel_cls = getattr(nexus_kernel, 'Kernel', None)
if kernel_cls is None:
    errors.append('  Kernel class is absent from module')
else:
    missing_methods = sorted(m for m in KERNEL_REQUIRED_METHODS if not hasattr(kernel_cls, m))
    if missing_methods:
        errors.append(f'  Kernel methods missing ({len(missing_methods)}): {missing_methods}')

if errors:
    print('FAIL — stale binary detected:')
    for e in errors:
        print(e)
    print('Fix: just setup')
    sys.exit(1)

print(f'OK — {len(MODULE_CAPABILITY_GROUPS)} capability groups, {len(KERNEL_REQUIRED_METHODS)} Kernel methods all present')
"

# Rebuild only kernel (fastest for Kernel-only changes).
build-kernel:
    uv run maturin develop --release -m rust/kernel/Cargo.toml

# Verify generated files (stubs, kernel_exports.py, _kernel_api_groups.py) are up-to-date.
codegen-check:
    uv run python scripts/codegen_kernel_abi.py --check

# Re-generate all codegen artifacts.
codegen:
    uv run python scripts/codegen_kernel_abi.py
