# Nexus Docker Sandbox Provider

Docker-based local sandbox provider for code execution with Nexus filesystem mounting.

## Overview

The Docker sandbox provider enables local container-based code execution as an alternative to cloud-based E2B sandboxes. It provides:

- **Local Development**: Run sandboxes without cloud dependencies or API keys
- **Direct Access**: Connect to `localhost:8080` (no ngrok needed)
- **Fast Mounting**: Reliable FUSE operations over local network
- **Easy Debugging**: Use `docker logs` and `docker exec` for troubleshooting
- **Cost Effective**: Free for local development

## Quick Start

### Prerequisites

1. Docker installed and running:
   ```bash
   docker --version
   ```

2. Nexus installed:
   ```bash
   pip install nexus-ai-fs
   ```

### Basic Usage

```bash
# Start Nexus server
nexus serve --host 0.0.0.0 --port 8080

# Create a Docker sandbox
nexus sandbox create my-sandbox --provider docker

# Run code
nexus sandbox run <sandbox-id> --code "print('Hello from Docker!')"

# Mount Nexus filesystem
nexus sandbox connect <sandbox-id> --provider docker --mount-path /mnt/nexus

# Stop sandbox
nexus sandbox stop <sandbox-id>
```

### Run Demo

```bash
# Run demo (builds nexus-runtime:latest automatically on first run)
./examples/cli/docker_sandbox_demo.sh

# Subsequent runs use cached image (fast)
./examples/cli/docker_sandbox_demo.sh

# Skip build and use python:3.11-slim (slower, auto-installs nexus CLI)
SKIP_BUILD=1 ./examples/cli/docker_sandbox_demo.sh

# Keep sandbox running after demo for inspection
KEEP=1 ./examples/cli/docker_sandbox_demo.sh
```

## Nexus Runtime Image

### Building the Image

Build the pre-configured Nexus runtime image with all dependencies:

```bash
# Easy way - using build script
./docker/build.sh

# Force rebuild (no cache)
./docker/build.sh --force

# Manual way
docker build -f docker/nexus-runtime.Dockerfile -t nexus-runtime:latest .
```

The image includes:
- Python 3.11
- Node.js 20
- Nexus CLI (for FUSE mounting)
- Non-root user with sudo access
- FUSE3 support

### Using the Image

```bash
# Create sandbox with Nexus runtime
nexus sandbox create my-sandbox \
  --provider docker \
  --template nexus-runtime:latest

# Or set as default in environment
export DOCKER_IMAGE=nexus-runtime:latest
```

### Image Details

- **Base**: `python:3.11-slim`
- **User**: `nexus` (UID 1000, non-root with sudo)
- **Capabilities**: `SYS_ADMIN` (required for FUSE)
- **Default Command**: `sleep infinity`
- **Size**: ~500MB (with all dependencies)

## Architecture

```
┌──────────────────┐
│  Host Machine    │
│                  │
│  ┌────────────┐  │
│  │ Nexus      │  │
│  │ Server     │  │
│  │ :8080      │  │
│  └────────────┘  │
│        ▲         │
│        │         │
│  host.docker     │
│  .internal       │
│        │         │
│  ┌─────┴──────┐  │
│  │ Container  │  │
│  │            │  │
│  │ /mnt/nexus │  │
│  │ (FUSE)     │  │
│  └────────────┘  │
└──────────────────┘
```

### Key Features

1. **Container Lifecycle**: Create, pause, resume, destroy
2. **Code Execution**: Python, JavaScript, Bash with timeout
3. **Resource Limits**: Configurable memory and CPU limits
4. **FUSE Mounting**: Mount Nexus filesystem at any path
5. **TTL Cleanup**: Automatic cleanup of expired containers
6. **Host Network**: Automatic `localhost` → `host.docker.internal` transformation

## Configuration

### Environment Variables

```bash
# Provider selection
export NEXUS_SANDBOX_PROVIDER=docker

# Docker settings
export DOCKER_IMAGE=python:3.11-slim          # Default image
export DOCKER_MEMORY_LIMIT=512m               # Memory limit
export DOCKER_CPU_LIMIT=1.0                   # CPU cores
export DOCKER_CLEANUP_INTERVAL=60             # Cleanup interval (seconds)
```

### Python API

```python
from nexus import NexusFilesystem, LocalBackend

# Basic usage
nx = NexusFilesystem(
    backend=LocalBackend(),
    sandbox_provider="docker"
)

# Custom configuration
nx = NexusFilesystem(
    backend=LocalBackend(),
    sandbox_provider="docker",
    sandbox_config={
        "docker_image": "nexus-runtime:latest",
        "memory_limit": "1g",
        "cpu_limit": 2.0,
        "cleanup_interval": 120
    }
)

# Create sandbox
sandbox = nx.sandbox_create(
    name="dev-env",
    ttl_minutes=30,
    template_id="python:3.11-slim"
)

# Run code
result = nx.sandbox_run(
    sandbox["sandbox_id"],
    language="python",
    code='print("Hello!")'
)

# Mount filesystem
mount = nx.sandbox_connect(
    sandbox["sandbox_id"],
    nexus_url="http://localhost:8080",
    nexus_api_key="sk-your-key"
)
```

## FUSE Mounting

### How It Works

1. Container created with `SYS_ADMIN` capability
2. Nexus CLI installed in container (if not present)
3. URL transformed: `localhost` → `host.docker.internal`
4. FUSE mount started in background
5. Verification via `ls` command

### Mount Process

```bash
# Inside container, this happens automatically:
sudo NEXUS_API_KEY=sk-xxx \
  nexus mount /mnt/nexus \
  --remote-url http://host.docker.internal:8080 \
  --allow-other &
```

### Troubleshooting

**Mount fails with "Connection refused"**:
- Ensure Nexus server is running on `0.0.0.0` (not `127.0.0.1`)
- Check server is accessible: `curl http://localhost:8080/health`

**FUSE errors**:
- Verify container has `SYS_ADMIN` capability
- Check `fuse3` is installed in image
- Ensure user has sudo access

**Files not visible**:
- Wait 2-3 seconds after mount for initialization
- Check mount log: `docker exec <container> cat /tmp/nexus-mount.log`
- Verify permissions on Nexus files

## Comparison: Docker vs E2B

| Feature | Docker Provider | E2B Provider |
|---------|----------------|--------------|
| **Network** | Direct localhost | Needs ngrok/public URL |
| **Cost** | Free | Pay per use |
| **Speed** | Fast (local) | Network latency |
| **Setup** | Docker install | API key required |
| **Debugging** | `docker exec` | Limited access |
| **Production** | Not recommended | Production-ready |
| **FUSE** | Fast, reliable | Timeout issues |
| **Isolation** | Docker containers | VMs (stronger) |

### When to Use Docker

✅ Local development and testing
✅ CI/CD with Docker available
✅ Fast iteration on sandbox features
✅ Testing FUSE mounting locally
✅ No internet connection

### When to Use E2B

✅ Production deployments
✅ Multi-tenant SaaS
✅ Need stronger isolation
✅ Docker not available
✅ Cloud-native architecture

## Security

### Container Isolation

- **Non-root user** by default (UID 1000)
- **Minimal capabilities** (only `SYS_ADMIN` for FUSE)
- **No privileged mode**
- **Resource limits** enforced
- **Network isolation** (bridge mode)

### Best Practices

1. **Don't use in production** for multi-tenant scenarios
2. **Limit resource usage** to prevent DoS
3. **Use custom images** with minimal software
4. **Monitor container count** to prevent leaks
5. **Regular cleanup** of stopped containers

### Resource Limits

```python
# Configure limits
provider = DockerSandboxProvider(
    memory_limit="512m",      # Max memory
    cpu_limit=1.0,           # Max 1 CPU core
)
```

## Development

### Running Tests

```bash
# Unit tests
pytest tests/unit/core/test_sandbox_docker_provider.py -v

# Integration test
./examples/cli/docker_sandbox_demo.sh

# With cleanup disabled
KEEP=1 ./examples/cli/docker_sandbox_demo.sh
```

### Debug Mode

```bash
# View container logs
docker logs <container-id>

# Execute commands directly
docker exec -it <container-id> bash

# Check FUSE mount
docker exec <container-id> mount | grep nexus
docker exec <container-id> ls -la /mnt/nexus
```

### Common Issues

**"docker: command not found"**:
```bash
# Install Docker
brew install docker  # macOS
# Or download from https://docker.com
```

**"Cannot connect to Docker daemon"**:
```bash
# Start Docker Desktop or Docker daemon
# macOS: Open Docker Desktop
# Linux: sudo systemctl start docker
```

**Container cleanup not working**:
```bash
# Manual cleanup
docker ps -a --filter "label=org.nexus.sandbox=true" -q | xargs docker rm -f
```

## Examples

### Example 1: Basic Code Execution

```python
from nexus import NexusFilesystem, LocalBackend

nx = NexusFilesystem(
    backend=LocalBackend(),
    sandbox_provider="docker"
)

# Create sandbox
sb = nx.sandbox_create("test")

# Run Python
result = nx.sandbox_run(
    sb["sandbox_id"],
    "python",
    "import sys; print(sys.version)"
)
print(result["stdout"])

# Cleanup
nx.sandbox_stop(sb["sandbox_id"])
```

### Example 2: File Processing

```python
# Create files in Nexus
nx.write("/data/input.csv", "name,age\nAlice,30\nBob,25")

# Create sandbox and mount
sb = nx.sandbox_create("data-processor")
nx.sandbox_connect(
    sb["sandbox_id"],
    nexus_url="http://localhost:8080",
    nexus_api_key=api_key
)

# Process file
code = """
import csv

with open('/mnt/nexus/data/input.csv') as f:
    reader = csv.DictReader(f)
    total_age = sum(int(row['age']) for row in reader)
    print(f'Total age: {total_age}')
"""

result = nx.sandbox_run(sb["sandbox_id"], "python", code)
print(result["stdout"])  # "Total age: 55"
```

### Example 3: Multi-Language Execution

```python
sb = nx.sandbox_create("multi-lang")

# Python
nx.sandbox_run(sb["sandbox_id"], "python", "print('Python')")

# JavaScript
nx.sandbox_run(sb["sandbox_id"], "javascript", "console.log('Node.js')")

# Bash
nx.sandbox_run(sb["sandbox_id"], "bash", "echo 'Bash'")
```

## Roadmap

### Phase 1: MVP ✅
- [x] Basic container lifecycle
- [x] Code execution (Python, JS, Bash)
- [x] Resource limits
- [x] TTL cleanup
- [x] CLI integration

### Phase 2: FUSE Integration ✅
- [x] FUSE mounting
- [x] Host network resolution
- [x] Auto-install nexus CLI
- [x] Mount verification

### Phase 3: Advanced Features (Future)
- [ ] Custom Dockerfile support
- [ ] Better error messages
- [ ] Performance optimizations
- [ ] GPU support (via nvidia-docker)

### Phase 4: Production Ready (Future)
- [ ] Security audit
- [ ] Comprehensive documentation
- [ ] Performance benchmarks
- [ ] Migration guide from E2B

## Contributing

See the [design document](../docs/design/docker-sandbox-provider.md) for architecture details.

## License

Same as Nexus project license.
