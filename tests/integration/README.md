# Integration Tests for Nexus

This directory contains integration tests for verifying Nexus functionality.

## Remote vs Local Parity Tests (Issue #243)

These tests verify that remote Nexus (client-server mode) works identically to embedded Nexus (local mode).

### Quick Start

```bash
# Quickest way - run the CLI test
python tests/integration/test_remote_parity_cli.py
```

### Available Test Suites

| Test Suite | Description | Use Case | Runtime |
|------------|-------------|----------|---------|
| **test_remote_parity_cli.py** | Simple standalone test | Quick verification, demos | ~5s |
| **test_remote_parity.py** | Comprehensive pytest suite | Development, CI/CD | ~40s |
| **test_remote_parity.sh** | FUSE-based bash test | Testing FUSE mounts | Varies |

### Test Results Summary

✅ **17/19 tests passing**
- All core operations work identically
- 2 tests skipped (features not in RPC server)

See [PARITY_TEST_RESULTS.md](./PARITY_TEST_RESULTS.md) for detailed results.

### What's Tested

#### ✅ Core Operations (All Pass)
- File operations: read, write, delete, rename, exists
- Directory operations: mkdir, rmdir, is_directory
- Discovery: list, glob, grep
- Edge cases: large files, binary data, Unicode, concurrent ops
- Metadata: etags, versions, OCC

#### ⚠️ Not in RPC Server (Skipped)
- Batch operations: `write_batch`
- Version tracking: `list_versions`, `get_version`, `rollback`, `diff_versions`
- Workspace snapshots: `workspace_snapshot`, `workspace_restore`, etc.

### Running Tests

#### Option 1: CLI Test (Recommended)
```bash
python tests/integration/test_remote_parity_cli.py
```

**Output:**
```
✓ Basic write/read
✓ Exists operation
✓ Delete operation
✓ List operation
✓ Glob operation
✓ Large file handling (1MB)
✓ Unicode content
✓ Binary data handling
✓ Directory operations
✓ Metadata handling

✓ All tests passed!
Remote Nexus behavior matches embedded Nexus.
```

#### Option 2: Pytest Suite
```bash
# Full test suite
uv run pytest tests/integration/test_remote_parity.py -v

# With coverage
uv run pytest tests/integration/test_remote_parity.py -v --cov

# Specific test
uv run pytest tests/integration/test_remote_parity.py::TestRemoteLocalParity::test_large_files -v
```

#### Option 3: Bash Test
```bash
./tests/integration/test_remote_parity.sh
```

**Requirements:**
- FUSE installed (`brew install macfuse` on macOS)
- May require elevated privileges

### Test Architecture

```
┌─────────────────┐         ┌─────────────────┐
│   Local Nexus   │         │  Remote Nexus   │
│   (embedded)    │         │ (client-server) │
└────────┬────────┘         └────────┬────────┘
         │                           │
         │  Same operations          │
         │  Same data                │
         │                           │
         ├───────────────────────────┤
                     │
              Compare Results
                     │
              ✓ or ✗ for each test
```

Each test:
1. Creates isolated local and remote filesystems
2. Performs identical operations on both
3. Compares results
4. Reports pass/fail

### Test Coverage

- **Basic operations**: 100% coverage
- **Edge cases**: Large files, binary, Unicode, concurrent
- **Error handling**: File not found, permissions, conflicts
- **Performance**: Comparative metrics (informational)

### Troubleshooting

**CLI test fails:**
- Check Python version >= 3.11
- Verify nexus installed: `pip show nexus-ai-fs`

**Pytest fails:**
- Check pytest installed: `pip show pytest`
- Use `uv run pytest` for isolated environment

**Bash test fails:**
- Check FUSE installed: `which fusermount` (Linux) or `ls /Library/Filesystems/macfuse.fs` (macOS)
- Check mount permissions
- Review logs in `/tmp/nexus-parity-test-*/`

### Development

To add new tests:

1. **Add to CLI test** (`test_remote_parity_cli.py`):
   ```python
   def test_my_feature(self):
       """Test description."""
       # ... test code ...
       self.print_result("My feature", passed, details)
   ```

2. **Add to pytest suite** (`test_remote_parity.py`):
   ```python
   def test_my_feature(self, test_env):
       """Test description."""
       local_nx = test_env["local"]
       remote_nx = test_env["remote"]
       # ... assertions ...
   ```

### Related Documentation

- [PARITY_TEST_RESULTS.md](./PARITY_TEST_RESULTS.md) - Detailed test results
- [Issue #243](https://github.com/nexi-lab/nexus/issues/243) - Original issue
- [Remote Client Docs](../../src/nexus/remote/client.py) - Remote FS implementation
- [RPC Server Docs](../../src/nexus/server/rpc_server.py) - Server implementation

### CI/CD Integration

Recommended command for CI:

```bash
# Fast test - runs in ~5 seconds
python tests/integration/test_remote_parity_cli.py

# Or comprehensive with coverage
uv run pytest tests/integration/test_remote_parity.py -v --cov
```

Exit code:
- `0` = all tests pass
- `1` = some tests failed

---

**Questions?** See issue #243 or contact the Nexus team.
