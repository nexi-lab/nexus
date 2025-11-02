# E2B Sandbox Template for Nexus AI FS

This directory contains an E2B sandbox template with Nexus AI FS pre-installed, allowing you to run AI agents with filesystem access in isolated cloud sandboxes.

## Overview

The template builds on top of the `e2bdev/code-interpreter:latest` base image and adds:
- FUSE (Filesystem in Userspace) support
- `fusepy` Python library
- `nexus-ai-fs` package

**Current Template ID**: `ty0ffopluq04os6yam4c`

## Quick Start

Use the automated setup script:

```bash
cd nexus/examples/e2b
./setup.sh              # Interactive build with local nexus source
./setup.sh --yes        # Non-interactive build with local source
./setup.sh --from-pypi  # Build using nexus-ai-fs from PyPI
```

### Build Modes

**Local Source Mode** (default):
- Copies your local `nexus` package into the Docker build context
- Installs with `pip install -e /app/nexus`
- Useful for development and testing local changes

**PyPI Mode** (`--from-pypi`):
- Installs the released version from PyPI with `pip install nexus-ai-fs`
- Faster build (no need to copy local source)
- Useful for production deployments or testing released versions

The script will:
- Check if E2B CLI is installed
- Authenticate with E2B (if needed)
- Build and deploy the template with the selected mode
- Display the template ID and usage examples

## Prerequisites

Install the E2B CLI:

```bash
# Using Homebrew (macOS)
brew install e2b

# Or using NPM
npm i -g @e2b/cli
```

## Authentication

Authenticate with E2B before building templates:

```bash
e2b auth login
```

This will open your browser for authentication. Once complete, your credentials will be saved locally.

## Building the Template

### Automated Setup (Recommended)

Use the [setup.sh](setup.sh) script for automated setup:

```bash
./setup.sh
```

### Manual Setup

If you prefer to set up manually:

#### Initial Setup

If starting from scratch:

```bash
# Initialize a new template (creates e2b.Dockerfile and e2b.toml)
e2b template init
```

#### Build and Deploy

Build the template from the [e2b.Dockerfile](e2b.Dockerfile):

```bash
# Build with the startup command
e2b template build -c "/root/.jupyter/start-up.sh"

# Or simply (uses e2b.toml configuration)
e2b template build
```

The build process will:
1. Build the Docker image locally
2. Push it to E2B's cloud infrastructure
3. Create a micro VM snapshot
4. Return a template ID

#### Updating the Template

After modifying [e2b.Dockerfile](e2b.Dockerfile), rebuild:

```bash
e2b template build
```

The template ID remains the same, but sandboxes created afterward will use the updated version.

## Using the Template

### Python

```python
from e2b import Sandbox, AsyncSandbox

# Sync sandbox
sandbox = Sandbox.create("ty0ffopluq04os6yam4c")

# Async sandbox
sandbox = await AsyncSandbox.create("ty0ffopluq04os6yam4c")

# Use Nexus AI FS in the sandbox
result = sandbox.process.start_and_wait("nexus --help")
print(result.stdout)
```

### JavaScript/TypeScript

```javascript
import { Sandbox } from 'e2b'

const sandbox = await Sandbox.create('ty0ffopluq04os6yam4c')

// Use Nexus AI FS in the sandbox
const result = await sandbox.process.startAndWait('nexus --help')
console.log(result.stdout)
```

## Dockerfile Details

The [e2b.Dockerfile](e2b.Dockerfile) contains:

```dockerfile
FROM e2bdev/code-interpreter:latest

# Install FUSE for filesystem operations
RUN apt-get update && \
    apt-get install -y fuse libfuse2 && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install fusepy
RUN pip install nexus-ai-fs
```

**Note**: Only Debian-based images (Debian, Ubuntu, or E2B images) are supported.

## Configuration

The [e2b.toml](e2b.toml) file contains template configuration:

- **template_id**: `ty0ffopluq04os6yam4c` (generated during first build)
- **team_id**: `2550f86d-a39b-4b9e-b551-14d7933b55bc`
- **dockerfile**: `e2b.Dockerfile`
- **start_cmd**: `/root/.jupyter/start-up.sh` (runs on sandbox creation)

## How It Works

E2B templates work by:

1. Building a Docker container from your Dockerfile
2. Extracting the entire filesystem state
3. Executing the start command
4. Saving a micro VM snapshot with all processes running
5. Sandboxes launch in ~few hundred milliseconds with everything pre-loaded

This means Nexus AI FS is already installed and ready to use when the sandbox starts!

## Resources

- [E2B Documentation](https://e2b.dev/docs)
- [E2B Sandbox Template Guide](https://e2b.dev/docs/sandbox-template)
- [Nexus AI FS on PyPI](https://pypi.org/project/nexus-ai-fs/)
- [E2B Python SDK](https://github.com/e2b-dev/e2b)

## Troubleshooting

### Build Fails

```bash
# Check E2B CLI version
e2b --version

# Re-authenticate
e2b auth login

# Verbose build output
e2b template build --verbose
```

### Template Not Found

Ensure you're using the correct template ID from [e2b.toml](e2b.toml). The template ID is generated on first build and remains constant.

### FUSE Permissions

E2B sandboxes run in privileged mode by default, so FUSE should work without additional configuration. If you encounter permission issues, check the sandbox logs.
