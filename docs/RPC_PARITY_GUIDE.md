# RPC Parity Guide

**Problem**: How to ensure parity between local NexusFS and RemoteNexusFS implementations?

When adding a new method to NexusFS, you need to update **two places**:
1. Core implementation (`src/nexus/core/nexus_fs*.py`) with `@rpc_expose` decorator
2. Remote client (`src/nexus/remote/client.py`) with corresponding RPC call

This was error-prone and easy to forget. **This is now automatically enforced!**

---

## 🔒 Automatic Enforcement (NEW!)

**All public methods MUST be either:**
1. ✅ Decorated with `@rpc_expose` (exposed via RPC), OR
2. ✅ Explicitly listed in `INTERNAL_ONLY_METHODS` (with justification)

**The test `test_all_public_methods_are_exposed_or_excluded()` will FAIL if:**
- You add a new public method to NexusFS without `@rpc_expose`
- AND it's not in the `INTERNAL_ONLY_METHODS` exclusion list

**This runs in CI and blocks PRs automatically!**

---

## Solution: Automated Parity Testing

**We have two automated tests**: `tests/unit/test_rpc_parity.py`

### Test 1: Enforcement Test (NEW!)
- ✅ **Finds ALL public methods** in NexusFS
- ✅ **Ensures each is either exposed or excluded**
- ✅ **Fails immediately** if new methods are missing decoration
- ✅ **Blocks PRs in CI** automatically

### Test 2: Implementation Parity Test
- ✅ Finds all methods decorated with `@rpc_expose` in NexusFS
- ✅ Verifies each one has a corresponding method in RemoteNexusFS
- ✅ Reports missing methods with file locations
- ✅ Runs automatically in CI

### Run the Test

```bash
# Quick check
uv run pytest tests/unit/test_rpc_parity.py -v

# With detailed output
uv run pytest tests/unit/test_rpc_parity.py -v -s
```

**Output example**:
```
✓ All 26 @rpc_expose methods have RemoteNexusFS implementations
  Exposed methods: batch_get_content_ids, chgrp, chmod, chown, delete...
  Remote methods: 28 total
```

If a method is missing, the test fails with:
```
The following @rpc_expose methods are missing from RemoteNexusFS:
  - new_method() [nexus_fs.py:123]

To fix this:
1. Add the missing method(s) to src/nexus/remote/client.py
2. Each method should call self._call_rpc(method_name, params)
3. See existing methods in RemoteNexusFS for examples
```

---

## How to Add a New RPC Method

### Step 1: Add Method to Core with `@rpc_expose`

```python
# src/nexus/core/nexus_fs.py (or any mixin)
from nexus.core.rpc_decorator import rpc_expose

@rpc_expose(description="Your method description")
def your_new_method(self, path: str, param1: str, param2: int = 0) -> dict[str, Any]:
    """Your method docstring."""
    # Implementation
    return {"result": "value"}
```

### Step 2: Add Corresponding Client Method

```python
# src/nexus/remote/client.py
def your_new_method(
    self,
    path: str,
    param1: str,
    param2: int = 0,
    context: Any = None  # Optional, handled server-side
) -> dict[str, Any]:  # noqa: ARG002
    """Your method docstring (can copy from core).

    Args:
        path: Description
        param1: Description
        param2: Description
        context: Unused in remote client (handled server-side)

    Returns:
        Description
    """
    result = self._call_rpc("your_new_method", {
        "path": path,
        "param1": param1,
        "param2": param2,
    })
    return result  # type: ignore[no-any-return]
```

### Step 3: Run Parity Test

```bash
uv run pytest tests/unit/test_rpc_parity.py -v
```

✅ If it passes, you're done!
❌ If it fails, add the missing method to RemoteNexusFS.

---

## Design Patterns

### Pattern 1: Simple RPC Call
For methods that return simple types or dicts:

```python
def simple_method(self, param: str) -> dict[str, Any]:
    """Simple method."""
    result = self._call_rpc("simple_method", {"param": param})
    return result  # type: ignore[no-any-return]
```

### Pattern 2: Extracting Nested Results
For methods where RPC returns `{"key": value}`:

```python
def list_method(self, path: str) -> list[str]:
    """List files."""
    result = self._call_rpc("list", {"path": path})
    return result["files"]  # type: ignore[no-any-return]
```

### Pattern 3: Methods with Context Parameter
Remote client doesn't use `context` (handled server-side):

```python
def chmod(self, path: str, mode: int, context: Any = None) -> None:  # noqa: ARG002
    """Change permissions.

    Args:
        path: File path
        mode: Permission mode
        context: Unused in remote client (handled server-side)
    """
    self._call_rpc("chmod", {"path": path, "mode": mode})
```

### Pattern 4: Return Raw Bytes
For methods returning bytes:

```python
def read(self, path: str) -> bytes:
    """Read file content."""
    result = self._call_rpc("read", {"path": path})
    return result  # type: ignore[no-any-return]
```

---

## Adding CLI Commands (Optional)

If your method should be accessible via CLI:

### Add Command File
```python
# src/nexus/cli/commands/your_feature.py
import click
from nexus.cli.utils import get_filesystem, handle_error

@click.command(name="your-command")
@click.argument("path", type=str)
@click.option("--flag", is_flag=True, help="Your flag")
def your_command(path: str, flag: bool) -> None:
    """CLI command description."""
    try:
        nx = get_filesystem(backend_config)
        result = nx.your_new_method(path, flag)
        print(result)
        nx.close()
    except Exception as e:
        handle_error(e)
```

### Register Command
```python
# src/nexus/cli/__init__.py
from nexus.cli.commands import your_feature

# In create_cli():
your_feature.register_commands(cli)
```

---

## Testing Checklist

When adding a new RPC method, verify:

- [ ] Method has `@rpc_expose` decorator in core
- [ ] RemoteNexusFS has corresponding client method
- [ ] Unit test: `pytest tests/unit/test_rpc_parity.py` passes
- [ ] Integration test: Add test to `tests/integration/test_remote_parity.py`
- [ ] CLI test (if applicable): Add test to `tests/integration/test_remote_parity.sh`
- [ ] Documentation: Update method docstrings

---

## CI Integration

✅ **Already integrated!** The RPC parity check runs automatically in CI:

```yaml
jobs:
  rpc-parity:
    name: RPC Parity Check
    runs-on: ubuntu-latest
    steps:
      # ... setup steps ...
      - name: Check RPC Parity (ENFORCEMENT)
        run: uv run pytest tests/unit/test_rpc_parity.py -v
```

**This blocks all PRs that add new public methods without proper RPC exposure!**

---

## Enforcement Policy

### When Adding a New Method

**You MUST choose ONE of these options:**

#### Option 1: Expose via RPC (RECOMMENDED - 95% of cases)

```python
from nexus.core.rpc_decorator import rpc_expose

@rpc_expose(description="Your method description")
def your_new_method(self, path: str, param: str) -> dict:
    """Docstring."""
    # Implementation
    return {"result": "value"}
```

Then add corresponding RemoteNexusFS implementation (see above).

#### Option 2: Mark as Internal-Only (RARE - 5% of cases)

If the method should **NOT** be exposed remotely, add it to `INTERNAL_ONLY_METHODS`:

```python
# In tests/unit/test_rpc_parity.py
INTERNAL_ONLY_METHODS = {
    # ... existing methods ...

    # Your new internal method
    "your_internal_method",  # Reason: Connection management / Async only / etc.
}
```

**Valid reasons for internal-only:**
- ❌ **Invalid**: "I forgot to add @rpc_expose"
- ❌ **Invalid**: "I didn't think it was important"
- ✅ **Valid**: "This is a connection lifecycle method (close, connect, etc.)"
- ✅ **Valid**: "This requires async RPC support which we don't have yet"
- ✅ **Valid**: "This is a local-only optimization method"

### CI Failure Messages

If you see this error in CI:

```
❌ ENFORCEMENT FAILURE: The following public methods are NOT @rpc_expose decorated
   and NOT in the INTERNAL_ONLY_METHODS exclusion list:

  - your_new_method() [nexus_fs.py:123]
```

**Fix it by:**
1. Adding `@rpc_expose` decorator (recommended)
2. Adding RemoteNexusFS implementation
3. OR adding to `INTERNAL_ONLY_METHODS` with justification (rare)

---

## Alternative: Code Generation (Future Enhancement)

To fully eliminate manual duplication, consider:

1. **Automatic Client Generation**
   - Generate RemoteNexusFS methods from `@rpc_expose` decorators
   - Tool: `scripts/generate_remote_client.py`

2. **Dynamic Proxy Pattern**
   - Auto-forward all `@rpc_expose` methods via `__getattr__`
   - Pros: Zero duplication
   - Cons: Less type safety, harder debugging

3. **Shared Interface**
   - Define protocol/interface that both must implement
   - Use type checking to verify compliance

For now, **manual implementation + automated testing** provides the best balance of:
- ✅ Type safety
- ✅ Clear error messages
- ✅ Easy debugging
- ✅ Automatic verification

---

## Summary

**To ensure parity going forward:**

1. ✅ **Automated test**: Run `tests/unit/test_rpc_parity.py` in CI
2. ✅ **Clear process**: Follow the "How to Add a New RPC Method" guide
3. ✅ **Integration tests**: Add comprehensive tests in `test_remote_parity.py`
4. ✅ **Documentation**: Keep this guide updated

**You DON'T need to manually track parity** - the test does it automatically! 🎉
