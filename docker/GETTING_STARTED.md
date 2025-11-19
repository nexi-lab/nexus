# Getting Started with Docker Sandbox Provider

Quick guide to using Docker-based sandboxes with Nexus.

## Prerequisites

1. **Docker installed and running**
   ```bash
   docker --version
   # Should show: Docker version 20.x or higher
   ```

2. **Nexus installed**
   ```bash
   pip install nexus-ai-fs
   ```

## Quick Start (3 steps)

### Step 1: Run the demo script

The demo script will automatically:
- Build the `nexus-runtime:latest` image (first run only)
- Start a local Nexus server
- Create a Docker sandbox
- Demonstrate FUSE mounting and code execution

```bash
./examples/cli/docker_sandbox_demo.sh
```

**First run**: Takes ~3-5 minutes (builds image)
**Subsequent runs**: Takes ~30 seconds (uses cached image)

### Step 2: Watch the demo

The script demonstrates:
- ✅ Creating test files in Nexus
- ✅ Creating a Docker sandbox
- ✅ Running Python, JavaScript, and Bash code
- ✅ Mounting Nexus filesystem via FUSE
- ✅ Reading/writing files from sandbox
- ✅ Pause/resume operations

### Step 3: Try it yourself

```bash
# Start Nexus server
nexus serve --host 0.0.0.0 --port 8080 &

# Create API key
export NEXUS_API_KEY=$(nexus admin create-user dev --name "Developer" --subject-type user | grep "API Key:" | awk '{print $3}')
export NEXUS_URL=http://localhost:8080

# Create a sandbox
SANDBOX_ID=$(nexus sandbox create my-sandbox --provider docker --ttl 30 --json | jq -r '.sandbox_id')

# Run some code
nexus sandbox run $SANDBOX_ID --code "print('Hello from Docker!')"

# Clean up
nexus sandbox stop $SANDBOX_ID
```

## Image Management

### The Nexus Runtime Image

The demo automatically builds `nexus-runtime:latest` which includes:

- **Python 3.11** - Latest Python runtime
- **Node.js 20** - Latest LTS Node.js
- **Nexus CLI** - Pre-installed for FUSE mounting
- **FUSE3** - Filesystem mounting support
- **Build tools** - gcc, make, etc. for compiling packages
- **Non-root user** - Runs as `nexus` user (UID 1000)

### Build Commands

```bash
# Build using convenience script
./docker/build.sh

# Force rebuild (no cache)
./docker/build.sh --force

# Check version
./docker/build.sh --version

# Manual build
docker build -f docker/nexus-runtime.Dockerfile -t nexus-runtime:latest .
```

### Image Size

- Base image (`python:3.11-slim`): ~150MB
- Nexus runtime: ~500MB (with Node.js, FUSE, build tools)

### Verify Image

```bash
# List images
docker images nexus-runtime

# Test image
docker run --rm nexus-runtime:latest python --version
docker run --rm nexus-runtime:latest node --version
docker run --rm nexus-runtime:latest nexus --version
```

## Usage Patterns

### Pattern 1: Quick Script Execution

```bash
# Create sandbox
SANDBOX=$(nexus sandbox create quick-test --provider docker --json)
SANDBOX_ID=$(echo $SANDBOX | jq -r '.sandbox_id')

# Run code
nexus sandbox run $SANDBOX_ID --code "
import sys
print(f'Python {sys.version}')
print('Hello from sandbox!')
"

# Clean up
nexus sandbox stop $SANDBOX_ID
```

### Pattern 2: Data Processing with Mounted Files

```bash
# Create data in Nexus
nexus mkdir /data --parents
nexus write /data/input.csv "name,value\nA,10\nB,20\nC,30"

# Create and mount sandbox
SANDBOX_ID=$(nexus sandbox create data-processor --provider docker --json | jq -r '.sandbox_id')
nexus sandbox connect $SANDBOX_ID --provider docker --mount-path /mnt/nexus

# Process data
nexus sandbox run $SANDBOX_ID --code "
import csv

with open('/mnt/nexus/data/input.csv') as f:
    reader = csv.DictReader(f)
    total = sum(int(row['value']) for row in reader)
    print(f'Total: {total}')

# Write results
with open('/mnt/nexus/data/output.txt', 'w') as f:
    f.write(f'Total: {total}')
"

# Read results
nexus cat /data/output.txt

# Clean up
nexus sandbox stop $SANDBOX_ID
```

### Pattern 3: Long-Running Development Environment

```bash
# Create sandbox with long TTL
SANDBOX_ID=$(nexus sandbox create dev-env --provider docker --ttl 120 --json | jq -r '.sandbox_id')

# Mount your workspace
nexus sandbox connect $SANDBOX_ID --provider docker --mount-path /mnt/nexus

# Use it for multiple tasks
nexus sandbox run $SANDBOX_ID --code "print('Task 1')"
nexus sandbox run $SANDBOX_ID --code "console.log('Task 2')" --language javascript

# Pause when not in use (saves resources)
nexus sandbox pause $SANDBOX_ID

# Resume later
nexus sandbox resume $SANDBOX_ID

# Continue working
nexus sandbox run $SANDBOX_ID --code "print('Task 3')"

# Clean up when done
nexus sandbox stop $SANDBOX_ID
```

## Environment Variables

```bash
# Demo script options
KEEP=1                     # Keep sandbox and server running after demo
SKIP_BUILD=1               # Skip image build, use python:3.11-slim
NEXUS_DATA_DIR=/path       # Custom data directory
DOCKER_IMAGE=image:tag     # Use custom Docker image

# Nexus configuration
NEXUS_URL=http://...       # Nexus server URL
NEXUS_API_KEY=sk-...       # API key for authentication
NEXUS_SANDBOX_PROVIDER=docker  # Default sandbox provider
```

## Troubleshooting

### Docker not running

```bash
# Error: Cannot connect to Docker daemon
# Solution: Start Docker Desktop or Docker service
```

### Port 8080 already in use

```bash
# Kill existing process
lsof -ti:8080 | xargs kill -9

# Or use different port
nexus serve --port 8081
export NEXUS_URL=http://localhost:8081
```

### Image build fails

```bash
# Check Docker is working
docker run hello-world

# Force rebuild
./docker/build.sh --force

# Check logs
docker build -f docker/nexus-runtime.Dockerfile -t nexus-runtime:latest . 2>&1 | tee build.log
```

### FUSE mount fails

```bash
# Check container has SYS_ADMIN capability
docker inspect <container-id> | grep -A 10 CapAdd

# Check server is accessible from container
docker exec <container-id> curl http://host.docker.internal:8080/health

# View mount logs
docker exec <container-id> cat /tmp/nexus-mount.log
```

### Sandbox not cleaning up

```bash
# List all Nexus sandboxes
nexus sandbox list

# Stop specific sandbox
nexus sandbox stop <sandbox-id>

# Manual Docker cleanup
docker ps -a | grep nexus
docker rm -f <container-id>
```

## Next Steps

- **Read full documentation**: [README.md](./README.md)
- **Design document**: [../docs/design/docker-sandbox-provider.md](../docs/design/docker-sandbox-provider.md)
- **Run tests**: `pytest tests/unit/core/test_sandbox_docker_provider.py`
- **Try E2B provider**: Use `--provider e2b` for cloud-based sandboxes

## Comparison with E2B

| Feature | Docker (Local) | E2B (Cloud) |
|---------|----------------|-------------|
| Setup | Install Docker | Get API key |
| Network | Direct localhost | Need public URL |
| Cost | Free | Pay per use |
| Speed | Fast (local) | Network latency |
| FUSE | Reliable | Can timeout |
| Debug | Easy (docker exec) | Limited |
| Production | Not recommended | Recommended |

**Use Docker for**: Local development, testing, CI/CD
**Use E2B for**: Production, multi-tenant SaaS, cloud deployments

## Support

- **Issues**: https://github.com/nexi-lab/nexus/issues
- **Design**: Issue #389 - Add Docker provider
- **Docs**: `docker/README.md`
