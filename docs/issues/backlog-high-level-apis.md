## Summary

Consider adding optional high-level APIs that combine common file operation patterns for convenience.

## Proposed APIs

### 1. Blocking Read with Wait

```python
# Current (composable)
change = await nexus.wait_for_changes("/inbox/", timeout=30)
if change:
    content = nexus.read(change["path"])

# Proposed (integrated)
content = await nexus.read("/inbox/file.txt", wait=True, timeout=30)
```

### 2. Auto-locking Write

```python
# Current (composable)
lock_id = await nexus.lock("/file.txt")
if lock_id:
    try:
        nexus.write("/file.txt", content)
    finally:
        await nexus.unlock(lock_id, "/file.txt")

# Proposed (integrated)
await nexus.write("/file.txt", content, lock=True)
```

### 3. Auto-locking Read

```python
# Proposed
content = await nexus.read("/file.txt", lock=True)
```

## Decision Rationale (Backlogged)

After discussion, we decided to **keep composable primitives** as the primary API because:

1. **AI agents handle composition well** - These are standard programming patterns
2. **More flexibility** - Agents can insert logic between operations
3. **Better transparency** - Agents understand exactly what is happening
4. **Control** - Agents have full control over error handling and retry logic

## When to Revisit

- If user feedback indicates the composable API is too verbose
- If common usage patterns emerge that would benefit from shortcuts
- If AI agents consistently struggle with the composition

## Related

- Issue #1106 Block 2: GlobalEventBus - Distributed Event System
- Composable primitives: `wait_for_changes()`, `lock()`, `unlock()`, `extend_lock()`, `read()`, `write()`
