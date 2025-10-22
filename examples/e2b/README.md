# E2B + Nexus Integration

Mount Nexus filesystem in [E2B](https://e2b.dev/) sandboxes using FUSE, enabling AI agents to use standard Unix tools on remote Nexus files.

## Overview

This integration allows you to mount a remote Nexus server as a local filesystem within E2B sandboxes. Once mounted, all standard Unix tools and programming languages can work with Nexus files transparently.

### Benefits

- **Use any Unix tool**: `ls`, `cat`, `grep`, `find`, `vim`, `git`, `rsync`, `tar`, etc.
- **No special API needed**: Standard file I/O works in any language
- **Perfect for AI agents**: Agents can use familiar bash commands
- **Seamless integration**: Existing scripts and tools work without modification

## Quick Start

```bash
# 1. Set environment variables
export E2B_API_KEY=your-e2b-key
export NEXUS_API_KEY=your-nexus-key  # Optional

# 2. Build E2B template
e2b template build -c examples/e2b/Dockerfile --name nexus-sandbox-v1

# 3. Run demo
python examples/e2b/e2b_demo.py
# OR
bash examples/e2b/e2b_demo.sh
```

## Prerequisites

### 1. E2B Account and CLI

```bash
# Install E2B CLI
npm install -g @e2b/cli

# Login to E2B
e2b auth login

# Get your API key
export E2B_API_KEY=$(e2b auth whoami --json | jq -r '.apiKey')
```

### 2. Nexus Server Access

You can use the public Nexus server or run your own:

**Option A: Public Server**
```bash
export NEXUS_URL=http://nexus.sudorouter.ai
export NEXUS_API_KEY=your-api-key  # Contact team for access
```

**Option B: Local Server**
```bash
# Start local Nexus server
nexus serve --port 8080 --api-key mykey

# In another terminal, expose via ngrok
ngrok http 8080

# Use the ngrok URL
export NEXUS_URL=https://your-ngrok-url.ngrok.io
export NEXUS_API_KEY=mykey
```

### 3. Python Dependencies (for Python demo)

```bash
pip install e2b
```

### 4. System Tools (for bash demo)

```bash
# macOS
brew install jq

# Ubuntu/Debian
apt-get install jq
```

## Setup Instructions

### Step 1: Build E2B Template

The template pre-installs Nexus and FUSE in the E2B environment:

```bash
cd /path/to/nexus
e2b template build -c examples/e2b/Dockerfile --name nexus-sandbox-v1
```

This creates a template with:
- Python 3.11
- `nexus-ai-fs[fuse]` package
- FUSE3 support
- Pre-configured mount point at `/home/user/nexus`

### Step 2: Test the Template

```bash
# Create a test sandbox
SANDBOX_ID=$(e2b sandbox create --template nexus-sandbox-v1 | jq -r '.id')

# Test Nexus installation
e2b sandbox exec $SANDBOX_ID "nexus --version"

# Cleanup
e2b sandbox delete $SANDBOX_ID
```

## Usage

### Method 1: Using the Helper Script

The `mount_nexus.sh` script simplifies mounting:

```bash
# In E2B sandbox
NEXUS_URL=http://nexus.sudorouter.ai \
NEXUS_API_KEY=your-key \
./examples/e2b/mount_nexus.sh
```

### Method 2: Direct Mount Command

```bash
# In E2B sandbox
nexus mount /home/user/nexus \
  --remote http://nexus.sudorouter.ai \
  --api-key your-key \
  --daemon
```

### Method 3: Python SDK (e2b_demo.py)

```python
from e2b import Sandbox

sandbox = Sandbox(template="nexus-sandbox-v1")

# Mount Nexus
sandbox.commands.run(
    "nexus mount /home/user/nexus "
    "--remote http://nexus.sudorouter.ai "
    "--api-key your-key --daemon"
)

# Use standard file operations
sandbox.commands.run("echo 'Hello' > /home/user/nexus/workspace/file.txt")
result = sandbox.commands.run("cat /home/user/nexus/workspace/file.txt")
print(result.stdout)

# Cleanup
sandbox.commands.run("nexus unmount /home/user/nexus")
sandbox.close()
```

### Method 4: E2B CLI (e2b_demo.sh)

```bash
# Create sandbox
SANDBOX_ID=$(e2b sandbox create --template nexus-sandbox-v1 | jq -r '.id')

# Mount and use
e2b sandbox exec $SANDBOX_ID "nexus mount /home/user/nexus --remote http://nexus.sudorouter.ai --daemon"
e2b sandbox exec $SANDBOX_ID "echo 'Hello' > /home/user/nexus/workspace/file.txt"
e2b sandbox exec $SANDBOX_ID "cat /home/user/nexus/workspace/file.txt"

# Cleanup
e2b sandbox exec $SANDBOX_ID "nexus unmount /home/user/nexus"
e2b sandbox delete $SANDBOX_ID
```

## Examples

### Example 1: File Operations

```bash
# Create files
echo "Hello World" > /home/user/nexus/workspace/hello.txt

# Read files
cat /home/user/nexus/workspace/hello.txt

# List files
ls -lh /home/user/nexus/workspace/

# Copy files
cp /home/user/nexus/workspace/hello.txt /tmp/backup.txt
```

### Example 2: Search and Filter

```bash
# Search content
grep -r "TODO" /home/user/nexus/workspace/

# Find files
find /home/user/nexus -name "*.py" -type f

# Count lines
wc -l /home/user/nexus/workspace/*.txt
```

### Example 3: Python Script

```python
# Standard Python file I/O works!
with open('/home/user/nexus/workspace/data.json', 'w') as f:
    json.dump({'key': 'value'}, f)

with open('/home/user/nexus/workspace/data.json', 'r') as f:
    data = json.load(f)
```

### Example 4: Git Operations

```bash
cd /home/user/nexus/workspace
git init
git add .
git commit -m "Work from E2B sandbox"
```

### Example 5: AI Agent Use Case

```python
from e2b import Sandbox

def ai_agent_task(code: str):
    """Execute AI-generated code with access to Nexus files."""
    sandbox = Sandbox(template="nexus-sandbox-v1")

    # Mount Nexus
    sandbox.commands.run(
        "nexus mount /home/user/nexus --remote http://nexus.sudorouter.ai --daemon"
    )

    # Execute AI-generated code
    result = sandbox.commands.run(f"python3 -c '{code}'")

    # Cleanup
    sandbox.commands.run("nexus unmount /home/user/nexus")
    sandbox.close()

    return result.stdout

# AI-generated code can access Nexus transparently
ai_code = """
import pandas as pd
df = pd.read_csv('/home/user/nexus/workspace/data.csv')
result = df.describe()
result.to_csv('/home/user/nexus/workspace/summary.csv')
print('Analysis complete')
"""

output = ai_agent_task(ai_code)
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  E2B Sandbox (Ubuntu Container)                     │
│                                                      │
│  ┌────────────────────────────────────────────┐    │
│  │  /home/user/nexus (FUSE Mount Point)       │    │
│  │  ├── workspace/                             │    │
│  │  ├── shared/                                │    │
│  │  └── external/                              │    │
│  └────────────┬───────────────────────────────┘    │
│               │                                      │
│  ┌────────────▼───────────────────────────────┐    │
│  │  Nexus FUSE Client                         │    │
│  │  - Intercepts POSIX file operations        │    │
│  │  - Translates to Nexus RPC calls           │    │
│  └────────────┬───────────────────────────────┘    │
└───────────────┼──────────────────────────────────┘
                │
                │ HTTP/JSON-RPC
                │
┌───────────────▼──────────────────────────────────┐
│  Nexus Server (nexus.sudorouter.ai)              │
│  - Handles RPC requests                          │
│  - Manages file storage                          │
│  - Provides authentication                       │
└──────────────────────────────────────────────────┘
```

## Troubleshooting

### Mount Failed

```bash
# Check if FUSE is available
ls /dev/fuse

# Check Nexus installation
nexus --version

# Check server connectivity
curl http://nexus.sudorouter.ai/health

# Enable debug logging
nexus mount /home/user/nexus --remote http://nexus.sudorouter.ai --debug
```

### Permission Denied

E2B sandboxes may require FUSE permissions. If mounting fails:

1. Check E2B template configuration for `--device /dev/fuse --cap-add SYS_ADMIN`
2. Contact E2B support for FUSE enablement
3. Use fallback: Nexus CLI commands instead of FUSE mount

### Slow Performance

```bash
# Mount with caching enabled (default)
nexus mount /home/user/nexus --remote http://nexus.sudorouter.ai

# Check network latency
ping nexus.sudorouter.ai
```

### Unmount Issues

```bash
# Force unmount
nexus unmount /home/user/nexus --force

# Or use fusermount
fusermount -u /home/user/nexus

# Check if still mounted
mount | grep nexus
```

## Advanced Configuration

### Custom Mount Options

```bash
# Mount in binary mode (no parsing)
nexus mount /home/user/nexus \
  --remote http://nexus.sudorouter.ai \
  --mode binary

# Mount in text mode (parse all files)
nexus mount /home/user/nexus \
  --remote http://nexus.sudorouter.ai \
  --mode text

# Mount with debugging
nexus mount /home/user/nexus \
  --remote http://nexus.sudorouter.ai \
  --debug
```

### Environment Variables

```bash
# Set defaults
export NEXUS_URL=http://nexus.sudorouter.ai
export NEXUS_API_KEY=your-key
export MOUNT_PATH=/home/user/nexus
export E2B_TEMPLATE=nexus-sandbox-v1

# Use in scripts
./mount_nexus.sh
```

### Template Customization

Edit `Dockerfile` to add additional tools:

```dockerfile
FROM e2bdev/code-interpreter:latest

RUN apt-get update && apt-get install -y \
    python3.11 \
    python3-pip \
    fuse3 \
    git \
    vim \
    tree \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    nexus-ai-fs[fuse] \
    pandas \
    numpy

RUN mkdir -p /home/user/nexus
```

## Resources

- **Nexus Documentation**: [github.com/nexi-lab/nexus](https://github.com/nexi-lab/nexus)
- **E2B Documentation**: [e2b.dev/docs](https://e2b.dev/docs)
- **Issue #177**: [github.com/nexi-lab/nexus/issues/177](https://github.com/nexi-lab/nexus/issues/177)

## Contributing

Found an issue or have improvements? Please open an issue or PR on the [Nexus repository](https://github.com/nexi-lab/nexus).

## License

Apache 2.0 - See [LICENSE](../../LICENSE) for details.
