# Lifecycle Hooks

← [Plugins API](index.md)

Lifecycle hooks allow plugins to react to filesystem events in real-time. Hooks can inspect, modify, or cancel operations.

## Available Hook Types

```python
from nexus.plugins.hooks import HookType

# File Operations
HookType.BEFORE_WRITE    # Before writing a file
HookType.AFTER_WRITE     # After writing a file
HookType.BEFORE_READ     # Before reading a file
HookType.AFTER_READ      # After reading a file
HookType.BEFORE_DELETE   # Before deleting a file
HookType.AFTER_DELETE    # After deleting a file

# Directory Operations
HookType.BEFORE_MKDIR    # Before creating directory
HookType.AFTER_MKDIR     # After creating directory

# Other Operations
HookType.BEFORE_COPY     # Before copying file
HookType.AFTER_COPY      # After copying file
```

## Registering Hooks

Register hooks in your plugin's `hooks()` method:

```python
from nexus.plugins import NexusPlugin

class MyPlugin(NexusPlugin):
    def hooks(self) -> dict[str, Callable]:
        """Register lifecycle hooks."""
        return {
            "before_write": self.validate_content,
            "after_write": self.index_content,
            "before_delete": self.check_dependencies,
        }
```

## Hook Implementation

Hooks receive a context dictionary and can:
- Inspect the operation context
- Modify the context (for `before_*` hooks)
- Cancel the operation by returning `None`
- Log or perform side effects

### Hook Signature

```python
async def my_hook(self, context: dict) -> dict | None:
    """Hook handler.

    Args:
        context: Operation context dictionary

    Returns:
        - Modified context dict to continue
        - None to cancel the operation
    """
    # Hook logic here
    return context
```

## Hook Contexts

### BEFORE_WRITE / AFTER_WRITE

```python
context = {
    "path": str,              # File path
    "content": bytes,         # File content
    "if_match": str | None,   # Optional etag for OCC
    "if_none_match": bool,    # Create-only flag
    "result": dict | None,    # Result metadata (after_write only)
}
```

Example:

```python
async def validate_write(self, context: dict) -> dict | None:
    """Validate before writing."""
    path = context["path"]
    content = context["content"]

    # Check file size
    if len(content) > 10 * 1024 * 1024:  # 10MB
        print(f"Warning: Large file: {path} ({len(content)} bytes)")

    # Validate JSON files
    if path.endswith(".json"):
        import json
        try:
            json.loads(content)
        except json.JSONDecodeError:
            print(f"Error: Invalid JSON in {path}")
            return None  # Cancel write

    return context  # Continue

async def log_write(self, context: dict) -> dict:
    """Log after writing."""
    path = context["path"]
    result = context.get("result", {})
    etag = result.get("etag", "unknown")

    print(f"[{self.metadata().name}] Written: {path} (etag: {etag})")
    return context
```

### BEFORE_READ / AFTER_READ

```python
context = {
    "path": str,                # File path
    "return_metadata": bool,    # Whether to return metadata
    "content": bytes | None,    # File content (after_read only)
    "metadata": dict | None,    # File metadata (after_read only)
}
```

Example:

```python
async def track_read(self, context: dict) -> dict:
    """Track file reads."""
    path = context["path"]

    # Log access
    print(f"[{self.metadata().name}] Read: {path}")

    # Track in database
    self._record_access(path)

    return context

async def decrypt_content(self, context: dict) -> dict:
    """Decrypt content after reading."""
    path = context["path"]
    content = context.get("content")

    if path.endswith(".encrypted") and content:
        # Decrypt content
        decrypted = self._decrypt(content)
        context["content"] = decrypted

    return context
```

### BEFORE_DELETE / AFTER_DELETE

```python
context = {
    "path": str,  # File path
}
```

Example:

```python
async def check_dependencies(self, context: dict) -> dict | None:
    """Check dependencies before deleting."""
    path = context["path"]

    # Check if file is referenced
    if self._has_dependencies(path):
        print(f"Error: Cannot delete {path} - has dependencies")
        return None  # Cancel delete

    return context

async def cleanup_cache(self, context: dict) -> dict:
    """Cleanup cache after deleting."""
    path = context["path"]

    # Remove from cache
    if path in self._cache:
        del self._cache[path]
        print(f"Removed {path} from cache")

    return context
```

### BEFORE_MKDIR / AFTER_MKDIR

```python
context = {
    "path": str,         # Directory path
    "parents": bool,     # Create parent directories
    "exist_ok": bool,    # Don't error if exists
}
```

Example:

```python
async def validate_mkdir(self, context: dict) -> dict | None:
    """Validate directory creation."""
    path = context["path"]

    # Enforce naming conventions
    if not path.startswith("/workspace/"):
        print(f"Error: Directories must be under /workspace/")
        return None  # Cancel mkdir

    return context

async def log_mkdir(self, context: dict) -> dict:
    """Log directory creation."""
    path = context["path"]
    print(f"Created directory: {path}")
    return context
```

### BEFORE_COPY / AFTER_COPY

```python
context = {
    "source": str,      # Source path
    "destination": str, # Destination path
}
```

Example:

```python
async def validate_copy(self, context: dict) -> dict | None:
    """Validate file copy."""
    source = context["source"]
    destination = context["destination"]

    # Check quota
    if not self._has_quota(destination):
        print(f"Error: Quota exceeded for {destination}")
        return None  # Cancel copy

    return context
```

## Hook Priority

Hooks are executed in priority order (higher priority = executed first):

### Default Priority

```python
# Default priority is 0
def hooks(self) -> dict[str, Callable]:
    return {
        "before_write": self.my_hook  # priority = 0 (default)
    }
```

### Custom Priority

Configure priority in plugin configuration:

```yaml
# ~/.nexus/plugins/my-plugin/config.yaml
hook_priority:
  before_write: 10   # Higher priority
  after_write: 5
  before_read: -10   # Lower priority
```

### Execution Order

```
Hook Type: before_write

1. Plugin A (priority: 10)  ← Executed first
2. Plugin B (priority: 5)
3. Plugin C (priority: 0)   ← Default priority
4. Plugin D (priority: -10) ← Executed last
```

## Hook Chain

Hooks are executed in a chain - each hook receives the context from the previous hook:

```python
# Plugin A: before_write (priority: 10)
async def validate_size(self, context: dict) -> dict:
    content = context["content"]
    if len(content) > 1000000:
        context["is_large"] = True
    return context

# Plugin B: before_write (priority: 5)
async def compress_large_files(self, context: dict) -> dict:
    if context.get("is_large"):
        # Compress content
        context["content"] = compress(context["content"])
        context["compressed"] = True
    return context

# Plugin C: before_write (priority: 0)
async def log_write(self, context: dict) -> dict:
    if context.get("compressed"):
        print("Writing compressed file")
    return context
```

## Canceling Operations

Return `None` from a `before_*` hook to cancel the operation:

```python
async def prevent_overwrites(self, context: dict) -> dict | None:
    """Prevent overwriting important files."""
    path = context["path"]

    if path.startswith("/system/") and self.nx.exists(path):
        print(f"Error: Cannot overwrite system file: {path}")
        return None  # Cancel write

    return context
```

When a hook returns `None`:
- The operation is canceled
- Remaining hooks are not executed
- An error is returned to the caller

## Error Handling

Hooks should handle errors gracefully:

```python
async def safe_hook(self, context: dict) -> dict:
    """Hook with error handling."""
    try:
        # Process context
        path = context.get("path", "")
        # Do something...

        return context

    except Exception as e:
        # Log error
        print(f"Hook error in {self.metadata().name}: {e}")

        # Return original context (don't break the chain)
        return context
```

**Important**: If a hook raises an exception:
- The error is logged
- The hook chain continues with the next hook
- The original context is passed to the next hook

## Common Hook Patterns

### 1. Content Validation

```python
async def validate_content(self, context: dict) -> dict | None:
    """Validate file content."""
    path = context["path"]
    content = context["content"]

    # Validate JSON
    if path.endswith(".json"):
        import json
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            print(f"Invalid JSON in {path}: {e}")
            return None

    # Validate Python
    if path.endswith(".py"):
        import ast
        try:
            ast.parse(content.decode("utf-8"))
        except SyntaxError as e:
            print(f"Invalid Python in {path}: {e}")
            return None

    return context
```

### 2. Content Transformation

```python
async def auto_format(self, context: dict) -> dict:
    """Auto-format code files."""
    path = context["path"]
    content = context["content"]

    # Format Python files
    if path.endswith(".py"):
        import black
        formatted = black.format_str(
            content.decode("utf-8"),
            mode=black.Mode()
        )
        context["content"] = formatted.encode("utf-8")

    return context
```

### 3. Access Tracking

```python
async def track_access(self, context: dict) -> dict:
    """Track file access."""
    path = context["path"]

    # Log to database
    self._db.execute(
        "INSERT INTO access_log (path, timestamp) VALUES (?, ?)",
        (path, datetime.now())
    )

    return context
```

### 4. Content Indexing

```python
async def index_content(self, context: dict) -> dict:
    """Index content for search."""
    path = context["path"]
    result = context.get("result", {})

    # Extract metadata
    etag = result.get("etag")
    size = result.get("size")

    # Index for search
    self._search_index.add_document(
        path=path,
        etag=etag,
        size=size
    )

    return context
```

### 5. Quota Enforcement

```python
async def enforce_quota(self, context: dict) -> dict | None:
    """Enforce storage quota."""
    path = context["path"]
    content = context["content"]

    # Calculate user quota
    user = self._get_user_from_path(path)
    usage = self._get_user_usage(user)
    quota = self._get_user_quota(user)

    # Check quota
    if usage + len(content) > quota:
        print(f"Quota exceeded for {user}")
        return None  # Cancel write

    return context
```

### 6. Backup Creation

```python
async def create_backup(self, context: dict) -> dict:
    """Create backup before overwriting."""
    path = context["path"]

    # Check if file exists
    if self.nx and self.nx.exists(path):
        # Read current version
        old_content = self.nx.read(path)

        # Write backup
        backup_path = f"{path}.backup"
        self.nx.write(backup_path, old_content)
        print(f"Created backup: {backup_path}")

    return context
```

## Testing Hooks

### Unit Tests

```python
import pytest

@pytest.mark.asyncio
async def test_validate_hook():
    """Test validation hook."""
    from nexus_my_plugin import MyPlugin

    plugin = MyPlugin()

    # Test valid content
    context = {
        "path": "/test.json",
        "content": b'{"valid": "json"}'
    }
    result = await plugin.validate_content(context)
    assert result is not None

    # Test invalid content
    context = {
        "path": "/test.json",
        "content": b'invalid json'
    }
    result = await plugin.validate_content(context)
    assert result is None  # Should cancel
```

### Integration Tests

```python
@pytest.mark.asyncio
async def test_hook_integration(tmp_path):
    """Test hook with Nexus."""
    from nexus import connect
    from nexus.plugins import PluginRegistry
    from nexus_my_plugin import MyPlugin

    # Setup
    nx = connect(config={"data_dir": str(tmp_path)})
    registry = PluginRegistry(nx)

    # Register plugin
    plugin = MyPlugin(nx)
    await plugin.initialize({})
    registry.register(plugin)

    # Write file (should trigger hooks)
    nx.write("/test.json", b'{"test": "data"}')

    # Verify hook was executed
    # (check logs, database, etc.)

    nx.close()
```

## Best Practices

1. **Keep hooks lightweight** - They run on every operation
2. **Handle errors gracefully** - Don't break the hook chain
3. **Return original context on error** - Allows other hooks to continue
4. **Use appropriate hook type** - `before_*` for validation, `after_*` for side effects
5. **Document hook behavior** - Explain what your hook does and when it cancels operations
6. **Test thoroughly** - Test both success and failure cases
7. **Consider priority** - Higher priority for critical validation hooks

## Next Steps

- **[Plugin Registry](registry.md)** - Manage plugins and hooks
- **[Creating Plugins](creating-plugins.md)** - Build your first plugin
- **[Examples](examples.md)** - See real-world hook implementations

## See Also

- [Creating Plugins](creating-plugins.md)
- [Plugin Registry](registry.md)
- [Plugin Examples](examples.md)
