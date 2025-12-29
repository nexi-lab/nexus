# E2B Template for Nexus Server

This directory contains an E2B sandbox template configuration for the Nexus server. The template extends the official E2B `code-interpreter-v1` template with Nexus pre-installed and FUSE support, enabling stateful Python execution and filesystem mounting capabilities.

## Overview

The template uses E2B's **Build System 2.0** and provides:
- **Stateful Python execution** via Jupyter kernel on port 49999
- **FUSE support** for mounting Nexus filesystem
- **Nexus AI FS** pre-installed from GitHub
- All features of the official `code-interpreter-v1` template:
  - Python 3.12 with Jupyter
  - Node.js and npm
  - R, Deno, Bash, Java kernels
  - Matplotlib plotting support

## Template Configuration

### Template ID
- **Template ID:** `yf6wfidzfb6i7n7iawob`
- **Template Name:** `nexus-sandbox-latest`

This template ID is stable and used across all Nexus deployments.

## Building the Template

### Prerequisites

1. **Install Python dependencies:**
   ```bash
   pip install e2b
   ```

2. **Authenticate with E2B:**
   ```bash
   e2b auth login
   ```

   The API key will be stored in `~/.e2b/config.json`.

### Build Process

Build the template using the v2 build system:

```bash
cd nexus/e2b-template
python3 build.py
```

The build script will:
1. Read your E2B API key from `~/.e2b/config.json` or `E2B_API_KEY` environment variable
2. Build the template by extending `code-interpreter-v1`
3. Add FUSE support and install Nexus
4. Push to E2B cloud infrastructure
5. Output the template ID

**Note:** The template ID (`yf6wfidzfb6i7n7iawob`) remains constant across builds. Updates will be available immediately to new sandboxes.

## Template Architecture

### template.py

The `template.py` file defines the template using E2B's v2 Template SDK:

```python
from e2b import Template

template = (
    Template()
    .from_template("code-interpreter-v1")  # Inherit Jupyter server
    .set_user("root")
    .run_cmd("apt-get update && apt-get install -y fuse ...")  # Install FUSE
    .run_cmd("pip install fusepy nexus-ai-fs")  # Install packages
    .set_user("user")
    .set_workdir("/home/user")
)
```

**Key Features:**
- Extends official `code-interpreter-v1` template
- Inherits Jupyter server on port 49999 for stateful execution
- Adds FUSE support and Nexus installation
- Configures passwordless sudo for FUSE operations

### build.py

The build script programmatically builds and pushes the template:

```python
from e2b import Template
from template import template

result = Template.build(
    template,
    alias="nexus-sandbox-latest",
    api_key=api_key,
)
```

## Using the Template

### In Nexus Server

The Nexus server automatically uses this template for E2B sandboxes. Configure via environment variables:

```bash
export E2B_API_KEY="your-e2b-api-key"
export E2B_TEMPLATE_ID="yf6wfidzfb6i7n7iawob"  # Optional, this is the default
```

### With Nexus Client

```python
from nexus_client import RemoteNexusFS

nx = RemoteNexusFS("https://your-nexus-server.com", api_key="...")

# Create sandbox with E2B provider (uses template automatically)
sandbox = nx.sandbox_create(
    name="my-sandbox",
    provider="e2b"
)

# Run stateful Python code
result = sandbox.run_code("x = 42")
result = sandbox.run_code("print(x)")  # Variable persists across calls
```

### Direct E2B SDK Usage

```python
from e2b_code_interpreter import AsyncSandbox

# By template ID
sandbox = await AsyncSandbox.create("yf6wfidzfb6i7n7iawob")

# Or by template name
sandbox = await AsyncSandbox.create("nexus-sandbox-latest")

# Run stateful Python code
result = await sandbox.run_code("sum([1, 2, 3])")
print(result.text)  # Output: 6
```

## Updating the Template

To update the template with new dependencies or configurations:

1. **Edit `template.py`:**
   ```python
   # Add new packages or configuration
   .run_cmd("pip install new-package")
   ```

2. **Rebuild:**
   ```bash
   python3 build.py
   ```

3. **Verify:**
   ```python
   # Test the updated template
   sandbox = await AsyncSandbox.create("nexus-sandbox-latest")
   ```

The template ID remains the same, but new sandboxes will use the updated version immediately.

## Template Features

### Stateful Python Execution

The template includes a Jupyter kernel on port 49999 that enables stateful code execution:

```python
# Variables persist across run_code() calls
await sandbox.run_code("x = 42")
result = await sandbox.run_code("x + 8")  # Returns 50
```

### FUSE Support

Pre-configured for mounting Nexus filesystem:

```bash
# In the sandbox
nexus mount /home/user/nexus --api-key=...
ls /home/user/nexus  # Access Nexus files
```

### Installed Software

- **Python 3.12** (with Jupyter kernel)
- **Node.js 20.x** and npm
- **FUSE** (libfuse2, libfuse-dev, fusepy)
- **Nexus CLI** (`nexus-ai-fs` package)
- R, Deno, Bash, Java kernels (from code-interpreter-v1)

### User Configuration

- **User:** `user`
- **Home:** `/home/user`
- **Sudo:** Passwordless sudo enabled for FUSE operations
- **Mount Points:** `/home/user/nexus`, `/mnt/nexus`
- **FUSE Config:** `user_allow_other` enabled

## Troubleshooting

### Port 49999 Not Open

If you get "port is not open" errors, verify the template is using v2 build system:

```bash
# Check template build
e2b template list | grep nexus-sandbox-latest

# Rebuild if needed
python3 build.py
```

### Build Fails

```bash
# Verify E2B package version (should be 2.8.0+)
pip show e2b

# Check API key
cat ~/.e2b/config.json

# Re-authenticate if needed
e2b auth login
```

### Jupyter Kernel Not Working

The Jupyter server is inherited from `code-interpreter-v1`. To verify:

```python
from e2b_code_interpreter import AsyncSandbox

sandbox = await AsyncSandbox.create("nexus-sandbox-latest")
result = await sandbox.run_code("print('Hello')")
# Should work without "port not open" errors
```

### FUSE Mount Issues

```bash
# In sandbox, verify FUSE is installed
python3 -c "import fuse; print('OK')"

# Check sudo permissions
sudo -l

# Verify fuse.conf
cat /etc/fuse.conf | grep user_allow_other
```

## Migration from v1 Build System

This template has been migrated from E2B's deprecated v1 build system (Dockerfile + CLI) to the v2 build system (Python SDK).

**What Changed:**
- ❌ Removed: `e2b.Dockerfile`, `e2b.toml`, `e2b template build` CLI command
- ✅ Added: `template.py`, `build.py`, programmatic build via Python SDK
- ✅ Improved: Proper inheritance from `code-interpreter-v1` template
- ✅ Fixed: Jupyter server on port 49999 now works correctly

**Template ID:** Unchanged (`yf6wfidzfb6i7n7iawob`)

## Files

- **`template.py`** - Template definition using v2 SDK
- **`build.py`** - Build script for deploying template
- **`README.md`** - This documentation

## References

- [E2B Build System 2.0](https://e2b.dev/blog/introducing-build-system-2-0)
- [E2B Code Interpreter](https://github.com/e2b-dev/code-interpreter)
- [E2B Template SDK Documentation](https://e2b.dev/docs)
- [Nexus Sandbox Provider](../src/nexus/core/sandbox_e2b_provider.py)
