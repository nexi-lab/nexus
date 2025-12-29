# E2B Template for Nexus Server

This directory contains an E2B sandbox template configuration for the Nexus server. The template includes Nexus pre-installed with FUSE support, allowing sandboxes to mount the Nexus filesystem.

## Overview

The template is built from `e2b.Dockerfile` and configured via `e2b.toml`. It provides:
- Ubuntu 24.04 base image
- Python 3.13
- FUSE support (libfuse, fusepy)
- Nexus AI FS pre-installed
- Node.js and npm for JavaScript execution
- Proper user permissions for FUSE mounting

## Quick Start

### Prerequisites

1. **Install E2B CLI:**
   ```bash
   # macOS
   brew install e2b

   # Or via npm
   npm i -g @e2b/cli
   ```

2. **Authenticate with E2B:**
   ```bash
   e2b auth login
   ```

### Building the Template

Use the provided build script:

```bash
cd nexus/e2b-template
./build.sh
```

Or build manually:

```bash
e2b template build
```

The build process will:
1. Build the Docker image from `e2b.Dockerfile`
2. Push it to E2B's cloud infrastructure
3. Create a micro VM snapshot
4. Update `e2b.toml` with the template ID

## Configuration

### e2b.toml

The `e2b.toml` file contains template configuration:

```toml
team_id = "c95e0b17-985d-4f1c-88ef-858e33bf5b8b"
dockerfile = "e2b.Dockerfile"
template_name = "nexus-fuse-fix10"
template_id = "ohsk388peukvlesxaw6w"  # Generated on first build
```

**Note:** The `template_id` is generated automatically on the first build and remains constant for updates.

### e2b.Dockerfile

The Dockerfile installs:
- System dependencies (FUSE, Python 3.13, Node.js)
- Python packages (fusepy, nexus-ai-fs)
- User setup with sudo permissions for FUSE mounting

## Using the Template

### In Nexus Server

Set the template ID as an environment variable:

```bash
export E2B_API_KEY="your-e2b-api-key"
export E2B_TEMPLATE_ID="ohsk388peukvlesxaw6w"  # From e2b.toml
```

Or specify it when creating sandboxes:

```python
from nexus_client import RemoteNexusFS

nx = RemoteNexusFS("https://your-nexus-server.com", api_key="...")

# Create sandbox with specific template
sandbox = nx.sandbox_create(
    name="my-sandbox",
    provider="e2b",
    template_id="ohsk388peukvlesxaw6w"
)
```

### Direct E2B SDK Usage

```python
from e2b import AsyncSandbox

# By template ID
sandbox = await AsyncSandbox.create("ohsk388peukvlesxaw6w")

# Or by template name
sandbox = await AsyncSandbox.create("nexus-fuse-fix10")
```

## Updating the Template

After modifying `e2b.Dockerfile`, rebuild:

```bash
./build.sh
```

The template ID remains the same, but new sandboxes will use the updated version.

## Template Details

### Base Image
- Ubuntu 24.04

### Installed Software
- Python 3.13 (default python3)
- Node.js and npm
- FUSE (libfuse2, libfuse-dev)
- fusepy (from git)
- nexus-ai-fs (from GitHub main branch)

### User Setup
- User: `user`
- Home: `/home/user`
- Sudo: Passwordless sudo for FUSE mounting
- Mount points: `/home/user/nexus`, `/mnt/nexus`

### Features
- FUSE support enabled (`user_allow_other` in `/etc/fuse.conf`)
- Nexus CLI available as `nexus` command
- Python and JavaScript execution ready
- Bash shell available

## Troubleshooting

### Build Fails

```bash
# Check E2B CLI version
e2b --version

# Re-authenticate
e2b auth login

# Build with verbose output
./build.sh --verbose
```

### Template Not Found

- Verify the template ID in `e2b.toml`
- Check that you're authenticated: `e2b auth whoami`
- Ensure the template was built successfully

### FUSE Mount Issues

- Verify FUSE is installed: `python3 -c "import fuse; print('OK')"`
- Check user permissions: `sudo -l`
- Verify fuse.conf: `cat /etc/fuse.conf | grep user_allow_other`

## Related Files

- `e2b.Dockerfile` - Docker image definition
- `e2b.toml` - Template configuration
- `build.sh` - Build script

## References

- [E2B Documentation](https://e2b.dev/docs)
- [E2B Template Guide](https://e2b.dev/docs/sandbox-template)
- [Nexus Sandbox Provider](../src/nexus/core/sandbox_e2b_provider.py)
