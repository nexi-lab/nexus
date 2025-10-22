# E2B Integration Troubleshooting

## Common Issues

### "Error requesting template build: [undefined] no message"

This error typically indicates one of the following issues:

#### 1. **E2B Account Limitations**

Some E2B accounts may have restrictions on custom template building.

**Solution**: Contact E2B support or check your plan:
- Free tier accounts may have limited template build access
- Ensure your account has custom template permissions

#### 2. **E2B Service Issues**

The E2B API may be experiencing temporary issues.

**Solution**: Check E2B status and try again later:
```bash
# Check if E2B API is responding
curl https://api.e2b.dev/health
```

#### 3. **Workaround: Use Default E2B Sandbox + Runtime Installation**

If you cannot build custom templates, install Nexus at runtime:

```python
from e2b import Sandbox

# Use default code-interpreter template
sandbox = Sandbox(template="code-interpreter")

# Install Nexus in the running sandbox
sandbox.commands.run("pip install nexus-ai-fs[fuse]")

# Mount Nexus
sandbox.commands.run(
    "nexus mount /home/user/nexus "
    "--remote http://nexus.sudorouter.ai "
    "--daemon"
)

# Now use Nexus
sandbox.commands.run("echo 'test' > /home/user/nexus/workspace/file.txt")
result = sandbox.commands.run("cat /home/user/nexus/workspace/file.txt")
print(result.stdout)

# Cleanup
sandbox.commands.run("nexus unmount /home/user/nexus")
sandbox.close()
```

#### 4. **Alternative: Local Docker Testing**

Test the Dockerfile locally before building on E2B:

```bash
# Build locally
docker build -t nexus-sandbox examples/e2b/

# Run locally
docker run -it --device /dev/fuse --cap-add SYS_ADMIN nexus-sandbox

# Inside container, mount Nexus
nexus mount /home/user/nexus --remote http://nexus.sudorouter.ai --daemon
```

### Permission Errors with E2B CLI

If you see permission errors like `EACCES: permission denied, unlink '/Users/.../.e2b/config.json'`:

```bash
# Fix E2B config permissions
sudo chown -R $USER:$(id -gn $USER) ~/.e2b/

# Fix .config permissions (for CLI updates)
sudo chown -R $USER:$(id -gn $USER) ~/.config/
```

### HTML Response Instead of JSON

If `e2b template list` returns HTML error:

```bash
# Re-authenticate
e2b auth logout
e2b auth login

# Or manually set API key
export E2B_API_KEY=your-key-here
e2b template list
```

## Testing Without E2B Template

You can test the complete integration using the default E2B sandbox:

### 1. **Quick Test Script**

```bash
#!/bin/bash
# test_e2b_nexus_runtime.sh

export E2B_API_KEY=your-key
NEXUS_URL="http://nexus.sudorouter.ai"

# Create sandbox with default template
SANDBOX_ID=$(e2b sandbox create --template code-interpreter | jq -r '.id')
echo "Created sandbox: $SANDBOX_ID"

# Install Nexus
e2b sandbox exec $SANDBOX_ID "pip install nexus-ai-fs[fuse]"

# Mount Nexus
e2b sandbox exec $SANDBOX_ID "nexus mount /home/user/nexus --remote $NEXUS_URL --daemon"

# Test file operations
e2b sandbox exec $SANDBOX_ID "echo 'Hello from E2B' > /home/user/nexus/workspace/test.txt"
e2b sandbox exec $SANDBOX_ID "cat /home/user/nexus/workspace/test.txt"

# Cleanup
e2b sandbox exec $SANDBOX_ID "nexus unmount /home/user/nexus"
e2b sandbox delete $SANDBOX_ID
```

### 2. **Python Runtime Installation**

```python
#!/usr/bin/env python3
"""
Test E2B + Nexus without custom template
"""
from e2b import Sandbox
import os

def test_runtime_install():
    # Use default template
    sandbox = Sandbox(template="code-interpreter")
    print(f"✓ Sandbox created: {sandbox.id}")

    try:
        # Install Nexus
        print("Installing Nexus...")
        result = sandbox.commands.run("pip install -q nexus-ai-fs[fuse]")
        if result.exit_code != 0:
            print(f"✗ Install failed: {result.stderr}")
            return
        print("✓ Nexus installed")

        # Verify installation
        result = sandbox.commands.run("nexus --version")
        print(f"✓ Nexus version: {result.stdout.strip()}")

        # Mount Nexus
        print("Mounting Nexus...")
        nexus_url = os.getenv("NEXUS_URL", "http://nexus.sudorouter.ai")
        result = sandbox.commands.run(
            f"nexus mount /home/user/nexus --remote {nexus_url} --daemon"
        )
        if result.exit_code != 0:
            print(f"✗ Mount failed: {result.stderr}")
            return
        print("✓ Nexus mounted")

        # Test operations
        print("Testing file operations...")
        sandbox.commands.run("echo 'Test content' > /home/user/nexus/workspace/test.txt")
        result = sandbox.commands.run("cat /home/user/nexus/workspace/test.txt")
        print(f"✓ File content: {result.stdout.strip()}")

        print("\n=== All tests passed! ===")

    finally:
        sandbox.commands.run("nexus unmount /home/user/nexus 2>/dev/null || true")
        sandbox.close()
        print("✓ Cleanup complete")

if __name__ == "__main__":
    test_runtime_install()
```

## Getting Help

1. **E2B Support**:
   - Discord: https://discord.gg/e2b
   - GitHub: https://github.com/e2b-dev/E2B/issues

2. **Nexus Support**:
   - GitHub: https://github.com/nexi-lab/nexus/issues
   - Issue #177: https://github.com/nexi-lab/nexus/issues/177

3. **Check E2B Plan Limits**:
   ```bash
   e2b auth whoami
   ```

4. **Update E2B CLI**:
   ```bash
   npm update -g @e2b/cli
   ```

## Known Limitations

- **FUSE in E2B**: Some E2B configurations may not support FUSE mounts due to container security restrictions
- **Template Build Access**: Not all E2B accounts have custom template build permissions
- **Runtime Installation**: Installing packages at runtime adds ~30-60s overhead per sandbox creation

## Alternative Approaches

If E2B FUSE mounting doesn't work, consider:

1. **Use Nexus Native CLI** (without FUSE):
   ```bash
   nexus write /workspace/file.txt "content"
   nexus read /workspace/file.txt
   ```

2. **Use Nexus Python SDK**:
   ```python
   from nexus import connect
   nx = connect(remote_url="http://nexus.sudorouter.ai")
   nx.write("/workspace/file.txt", b"content")
   ```

3. **Sync Files Before/After**:
   - Download files from Nexus before E2B task
   - Upload results after E2B task completes
