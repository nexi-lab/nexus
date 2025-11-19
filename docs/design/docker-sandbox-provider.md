# Docker Sandbox Provider Design

**Issue:** [#389 - Add Docker provider for local sandbox execution](https://github.com/nexi-lab/nexus/issues/389)

**Status:** Design Phase

**Author:** System Design

**Date:** 2025-11-03

---

## Table of Contents

1. [Overview](#overview)
2. [Problem Statement](#problem-statement)
3. [Goals and Non-Goals](#goals-and-non-goals)
4. [Design Principles](#design-principles)
5. [Architecture](#architecture)
6. [Detailed Design](#detailed-design)
7. [Implementation Phases](#implementation-phases)
8. [API Design](#api-design)
9. [Configuration](#configuration)
10. [Security Considerations](#security-considerations)
11. [Testing Strategy](#testing-strategy)
12. [Learnings from OpenHands](#learnings-from-openhands)
13. [Alternatives Considered](#alternatives-considered)
14. [Open Questions](#open-questions)

---

## Overview

This document describes the design for a Docker-based sandbox provider for Nexus. The Docker provider will enable local container-based code execution as an alternative to cloud-based E2B sandboxes, improving local development experience and reducing costs.

## Problem Statement

### Current Limitations with E2B

E2B sandboxes run in the cloud (AWS) and face several challenges when used for local development:

1. **Network Access Issues:**
   - Cannot reliably access local Nexus instances through ngrok URLs
   - E2B infrastructure blocks/rate-limits ngrok free tier
   - Firewall and network restrictions from cloud providers

2. **Development Friction:**
   - Requires API key and internet connection
   - Incurs API usage costs during development
   - Higher latency (network round-trip to AWS)
   - Difficult to debug (no direct access to containers)

3. **FUSE Mounting Problems:**
   - Timeout errors when accessing mounted files
   - Connectivity issues with ngrok tunnels
   - Unreliable FUSE operations over WAN

### User Impact

Developers testing sandbox features with filesystem mounting experience:
- Frequent timeouts and connection errors
- Slow iteration cycles
- Unnecessary API costs during development
- Complex debugging workflow

## Goals and Non-Goals

### Goals

✅ **Local Development:**
- Run sandboxes locally without cloud dependencies
- Direct access to `localhost:8080` (no ngrok needed)
- Fast container startup (<5 seconds)

✅ **Feature Parity:**
- Implement complete `SandboxProvider` interface
- Support same API as E2B provider
- Compatible with existing sandbox CLI commands

✅ **Resource Management:**
- Configurable memory and CPU limits
- TTL-based auto-cleanup
- Support for pause/resume operations

✅ **Filesystem Integration:**
- FUSE mounting via `host.docker.internal`
- Reliable local file access
- Support for multiple mount points

✅ **Developer Experience:**
- Easy debugging with `docker logs` and `docker exec`
- Custom Docker images support
- Clear error messages and logging

### Non-Goals

❌ **Production Deployment:**
- Not designed for multi-tenant production use
- No advanced isolation beyond Docker containers
- No distributed orchestration (use E2B for cloud)

❌ **Advanced Orchestration:**
- No Kubernetes integration (future consideration)
- No container clustering
- No automatic scaling

❌ **GPU Support (Phase 1):**
- Basic CPU-only execution initially
- GPU support deferred to Phase 2

## Design Principles

1. **Interface Compatibility:** Follow existing `SandboxProvider` abstraction
2. **Security First:** Run as non-root user, minimal capabilities
3. **Resource Efficiency:** Clean up unused containers automatically
4. **Developer Friendly:** Simple configuration, clear errors
5. **Incremental Delivery:** MVP first, then advanced features

## Architecture

### Component Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Nexus Application                     │
└───────────────────────┬─────────────────────────────────┘
                        │
                        │ Uses SandboxProvider Interface
                        │
        ┌───────────────┴────────────────┐
        │                                │
        ▼                                ▼
┌──────────────────┐          ┌──────────────────┐
│  E2BSandbox      │          │  DockerSandbox   │
│  Provider        │          │  Provider (NEW)  │
└────────┬─────────┘          └────────┬─────────┘
         │                             │
         │ E2B API                     │ Docker SDK
         │                             │
         ▼                             ▼
┌──────────────────┐          ┌──────────────────┐
│  E2B Cloud       │          │  Docker Engine   │
│  (AWS)           │          │  (Local)         │
└──────────────────┘          └──────────────────┘
```

### Container Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Docker Container                      │
│  ┌────────────────────────────────────────────────────┐ │
│  │  Runtime Environment                               │ │
│  │  - Python 3.11                                     │ │
│  │  - Node.js (optional)                              │ │
│  │  - Bash shell                                      │ │
│  │  - nexus CLI (for FUSE mounting)                   │ │
│  └────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌────────────────────────────────────────────────────┐ │
│  │  FUSE Mount Point                                  │ │
│  │  /mnt/nexus → http://host.docker.internal:8080     │ │
│  └────────────────────────────────────────────────────┘ │
│                                                          │
│  User: nexus (non-root with sudo)                       │
│  Capabilities: SYS_ADMIN (for FUSE)                     │
│  Network: bridge mode                                   │
│  Resources: 512MB RAM, 1 CPU (configurable)             │
└─────────────────────────────────────────────────────────┘
```

### Network Architecture

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
│  │  Docker    │  │
│  │  Bridge    │  │
│  │  Network   │  │
│  └─────┬──────┘  │
│        │         │
│  ┌─────▼──────┐  │
│  │ Container  │  │
│  │            │  │
│  │ nexus      │  │
│  │ mount      │  │
│  │ /mnt/nexus │  │
│  └────────────┘  │
└──────────────────┘
```

## Detailed Design

### Class Structure

```python
# src/nexus/core/sandbox_docker_provider.py

class DockerSandboxProvider(SandboxProvider):
    """Docker-based local sandbox provider.

    Implements SandboxProvider interface using Docker containers for
    local code execution. Designed for development and testing.
    """

    def __init__(
        self,
        docker_client: docker.DockerClient | None = None,
        default_image: str = "python:3.11-slim",
        cleanup_interval: int = 60,
        auto_pull: bool = True
    ):
        """Initialize Docker sandbox provider.

        Args:
            docker_client: Docker client (defaults to docker.from_env())
            default_image: Default container image
            cleanup_interval: Seconds between cleanup checks
            auto_pull: Auto-pull missing images
        """
        self.docker_client = docker_client or docker.from_env()
        self.default_image = default_image
        self.cleanup_interval = cleanup_interval
        self.auto_pull = auto_pull

        # Cache for active containers
        self._containers: dict[str, ContainerInfo] = {}

        # Start cleanup task
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    # Interface implementation
    async def create(self, template_id, timeout_minutes, metadata) -> str
    async def run_code(self, sandbox_id, language, code, timeout) -> CodeExecutionResult
    async def pause(self, sandbox_id) -> None
    async def resume(self, sandbox_id) -> None
    async def destroy(self, sandbox_id) -> None
    async def get_info(self, sandbox_id) -> SandboxInfo
    async def is_available(self) -> bool
    async def mount_nexus(self, sandbox_id, mount_path, nexus_url, api_key) -> dict

    # Internal methods
    def _get_container(self, sandbox_id) -> docker.models.containers.Container
    async def _cleanup_loop(self) -> None
    def _build_command(self, language, code) -> list[str]
    def _ensure_image(self, image_name) -> None
```

### Container Information Model

```python
@dataclass
class ContainerInfo:
    """Internal container tracking information."""

    container: docker.models.containers.Container
    sandbox_id: str
    created_at: datetime
    expires_at: datetime
    template_id: str | None
    metadata: dict[str, Any]
    status: str  # "active", "paused", "stopped"
```

### Code Execution Flow

```
User Request
    │
    ▼
DockerSandboxProvider.run_code()
    │
    ├─► Validate language
    │
    ├─► Get container from cache
    │
    ├─► Build execution command
    │   ├─ Python: ["python", "-c", code]
    │   ├─ JavaScript: ["node", "-e", code]
    │   └─ Bash: ["bash", "-c", code]
    │
    ├─► Execute via container.exec_run()
    │   ├─ Set timeout
    │   ├─ Capture stdout/stderr
    │   └─ Get exit code
    │
    ├─► Measure execution time
    │
    └─► Return CodeExecutionResult
```

### FUSE Mounting Flow

```
User Request
    │
    ▼
DockerSandboxProvider.mount_nexus()
    │
    ├─► Get container
    │
    ├─► Transform URL
    │   └─ localhost:8080 → host.docker.internal:8080
    │
    ├─► Create mount directory
    │   └─ exec: mkdir -p /mnt/nexus
    │
    ├─► Check for nexus CLI
    │   ├─ Found: continue
    │   └─ Not found: pip install nexus-ai-fs
    │
    ├─► Run FUSE mount in background
    │   └─ sudo NEXUS_API_KEY=xxx nexus mount /mnt/nexus \
    │       --remote-url http://host.docker.internal:8080 \
    │       --allow-other &
    │
    ├─► Wait for initialization (2-3 seconds)
    │
    ├─► Verify mount
    │   └─ exec: ls -la /mnt/nexus
    │
    └─► Return mount status
        ├─ success: bool
        ├─ mount_path: str
        ├─ message: str
        └─ files_visible: int
```

### Container Lifecycle

```
CREATE
  │
  ├─► Pull image (if needed)
  │
  ├─► Run container
  │   ├─ Command: sleep infinity
  │   ├─ Detached: true
  │   ├─ Capabilities: [SYS_ADMIN]
  │   ├─ Network: bridge
  │   └─ Resources: configurable limits
  │
  ├─► Generate sandbox_id (container.id[:12])
  │
  ├─► Cache container info
  │
  └─► Return sandbox_id

PAUSE
  │
  └─► container.pause()

RESUME
  │
  └─► container.unpause()

DESTROY
  │
  ├─► Remove from cache
  │
  ├─► Stop container
  │
  └─► Remove container

CLEANUP (Background)
  │
  └─► Every 60 seconds:
      └─► For each container:
          └─► If expired:
              └─► Destroy
```

## Implementation Phases

### Phase 1: MVP (Week 1)

**Scope:** Basic Docker provider with essential features

**Deliverables:**
- [ ] `DockerSandboxProvider` class
- [ ] Basic container lifecycle (create, destroy)
- [ ] Code execution (Python, JavaScript, Bash)
- [ ] Resource limits (memory, CPU)
- [ ] TTL-based cleanup
- [ ] Unit tests
- [ ] Integration with existing CLI

**Acceptance Criteria:**
- Can create and destroy containers
- Can execute code with timeout
- Containers auto-cleanup after TTL
- Works with `nexus sandbox` commands

### Phase 2: FUSE Integration (Week 2)

**Scope:** Nexus filesystem mounting

**Deliverables:**
- [ ] `mount_nexus()` implementation
- [ ] Host network resolution (host.docker.internal)
- [ ] Auto-install nexus CLI in container
- [ ] Mount verification
- [ ] FUSE-specific container capabilities

**Acceptance Criteria:**
- Can mount local Nexus instance
- Files visible in container
- Read/write operations work
- No timeout errors

### Phase 3: Advanced Features (Week 3)

**Scope:** Pause/resume, custom images, better DX

**Deliverables:**
- [ ] Pause/resume support
- [ ] Custom Docker image support
- [ ] Dockerfile build support (optional)
- [ ] Better error messages
- [ ] Comprehensive logging
- [ ] Performance optimizations

**Acceptance Criteria:**
- Pause/resume preserves state
- Can use custom images
- Clear error messages for common issues
- <5 second container startup

### Phase 4: Production Ready (Week 4)

**Scope:** Hardening, documentation, examples

**Deliverables:**
- [ ] Security audit
- [ ] Resource leak prevention
- [ ] Comprehensive documentation
- [ ] Example scripts
- [ ] Performance benchmarks
- [ ] Migration guide from E2B

**Acceptance Criteria:**
- No resource leaks under load
- Documentation complete
- All tests passing
- Ready for release

## API Design

### Provider Initialization

```python
from nexus import NexusFS, LocalBackend

# Option 1: Use default Python image
nx = NexusFS(
    backend=LocalBackend(),
    sandbox_provider="docker"
)

# Option 2: Custom configuration
nx = NexusFS(
    backend=LocalBackend(),
    sandbox_provider="docker",
    sandbox_config={
        "docker_image": "python:3.11-slim",
        "memory_limit": "1g",
        "cpu_limit": 2.0,
        "cleanup_interval": 120
    }
)

# Option 3: Custom Docker client
import docker
client = docker.from_env()
nx = NexusFS(
    backend=LocalBackend(),
    sandbox_provider="docker",
    sandbox_config={"docker_client": client}
)
```

### Sandbox Operations

```python
# Create sandbox
sandbox = nx.sandbox_create(
    name="dev-env",
    ttl_minutes=30,
    template_id="python:3.11-slim"  # Docker image
)
# Returns: {"sandbox_id": "a1b2c3d4e5f6", "status": "active", ...}

# Run code
result = nx.sandbox_run(
    sandbox["sandbox_id"],
    language="python",
    code='print("Hello from Docker!")',
    timeout=10
)
# Returns: CodeExecutionResult(stdout="Hello from Docker!\n", ...)

# Mount Nexus filesystem
mount_result = nx.sandbox_connect(
    sandbox["sandbox_id"],
    nexus_url="http://localhost:8080",
    nexus_api_key="sk-your-key",
    mount_path="/mnt/nexus"
)
# Returns: {"success": True, "files_visible": 10, ...}

# Pause sandbox
nx.sandbox_pause(sandbox["sandbox_id"])

# Resume sandbox
nx.sandbox_resume(sandbox["sandbox_id"])

# Destroy sandbox
nx.sandbox_stop(sandbox["sandbox_id"])
```

### CLI Commands

```bash
# Set Docker as default provider
export NEXUS_SANDBOX_PROVIDER=docker

# Create sandbox with Docker
nexus sandbox create dev-env --provider docker --image python:3.11-slim

# Run code
nexus sandbox run <sandbox-id> python "print('Hello')"

# Mount local Nexus
nexus sandbox connect <sandbox-id> \
    --nexus-url http://localhost:8080 \
    --api-key sk-xxx

# List active containers
nexus sandbox list --provider docker

# Cleanup all expired containers
nexus sandbox cleanup --provider docker
```

## Configuration

### Environment Variables

```bash
# Provider selection
NEXUS_SANDBOX_PROVIDER=docker          # Use Docker provider

# Docker settings
DOCKER_IMAGE=python:3.11-slim          # Default image
DOCKER_MEMORY_LIMIT=512m               # Memory limit
DOCKER_CPU_LIMIT=1.0                   # CPU limit (cores)
DOCKER_CLEANUP_INTERVAL=60             # Cleanup interval (seconds)
DOCKER_AUTO_PULL=true                  # Auto-pull missing images

# Network settings
DOCKER_NETWORK_MODE=bridge             # Network mode
DOCKER_HOST_GATEWAY=host.docker.internal  # Host gateway name

# Security settings
DOCKER_RUN_AS_ROOT=false               # Run as non-root (recommended)
DOCKER_PRIVILEGED=false                # Privileged mode (not recommended)
DOCKER_CAPABILITIES=SYS_ADMIN          # Additional capabilities
```

### Configuration File

```yaml
# nexus.yaml
sandbox:
  provider: docker

  docker:
    default_image: python:3.11-slim

    resources:
      memory_limit: 512m
      cpu_limit: 1.0

    network:
      mode: bridge
      host_gateway: host.docker.internal

    security:
      run_as_root: false
      privileged: false
      capabilities:
        - SYS_ADMIN

    cleanup:
      interval: 60
      max_age_minutes: 60

    images:
      auto_pull: true
      cache_locally: true
```

## Security Considerations

### Container Isolation

**Non-Root User:**
```dockerfile
# Recommended Dockerfile pattern
FROM python:3.11-slim

RUN useradd -m -u 1000 nexus && \
    echo "nexus ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

USER nexus
WORKDIR /home/nexus
```

**Minimal Capabilities:**
```python
# Only add SYS_ADMIN for FUSE, not privileged mode
container = docker_client.containers.run(
    image=image,
    cap_add=['SYS_ADMIN'],  # FUSE only
    privileged=False,       # Never use privileged
    security_opt=['no-new-privileges'],
    read_only=False  # FUSE needs write access
)
```

### Resource Limits

**Memory Protection:**
```python
# Prevent OOM attacks
mem_limit="512m",
memswap_limit="512m",  # No swap
oom_kill_disable=False  # Allow OOM killer
```

**CPU Protection:**
```python
# Prevent CPU exhaustion
cpu_quota=100000,  # 1 CPU core
cpu_period=100000,
cpu_shares=1024
```

**Storage Protection:**
```python
# Limit disk usage
storage_opt={
    'size': '10G'  # Max container size
}
```

### Network Security

**Isolated Network:**
```python
# Use bridge network, not host
network_mode='bridge',

# No direct internet access (optional)
network_disabled=True,  # If internet not needed

# DNS restrictions
dns=['8.8.8.8'],
dns_search=[]
```

**Port Exposure:**
```python
# Never expose ports unless explicitly needed
ports={}  # No port mappings by default
```

### Secret Management

**API Keys:**
```python
# Never log API keys
logger.info(f"Mounting with api_key=***{api_key[-4:]}")

# Pass via environment, not command args
environment={
    'NEXUS_API_KEY': api_key
}

# Clean up environment after use
container.exec_run("unset NEXUS_API_KEY")
```

### Docker Socket Access

**Never Mount Docker Socket:**
```python
# NEVER DO THIS - security vulnerability
volumes={
    '/var/run/docker.sock': {  # ❌ DANGEROUS
        'bind': '/var/run/docker.sock'
    }
}
```

## Testing Strategy

### Unit Tests

```python
# tests/core/test_sandbox_docker_provider.py

class TestDockerSandboxProvider:
    async def test_create_sandbox(self):
        """Test sandbox creation."""
        provider = DockerSandboxProvider()
        sandbox_id = await provider.create()
        assert sandbox_id
        assert len(sandbox_id) == 12

    async def test_run_python_code(self):
        """Test Python code execution."""
        provider = DockerSandboxProvider()
        sandbox_id = await provider.create()

        result = await provider.run_code(
            sandbox_id,
            "python",
            "print('test')",
            timeout=5
        )

        assert result.stdout == "test\n"
        assert result.exit_code == 0

    async def test_cleanup_expired(self):
        """Test TTL-based cleanup."""
        provider = DockerSandboxProvider()
        sandbox_id = await provider.create(timeout_minutes=0)

        await asyncio.sleep(2)

        with pytest.raises(SandboxNotFoundError):
            await provider.get_info(sandbox_id)
```

### Integration Tests

```python
# tests/integration/test_sandbox_docker_e2e.py

class TestDockerSandboxE2E:
    async def test_full_workflow(self):
        """Test complete sandbox workflow."""
        nx = NexusFS(sandbox_provider="docker")

        # Create
        sandbox = nx.sandbox_create("test")
        assert sandbox["status"] == "active"

        # Execute code
        result = nx.sandbox_run(
            sandbox["sandbox_id"],
            "python",
            "import sys; print(sys.version)"
        )
        assert "3.11" in result.stdout

        # Mount Nexus
        nx.write("/test.txt", "Hello")
        mount = nx.sandbox_connect(
            sandbox["sandbox_id"],
            nexus_url="http://localhost:8080",
            nexus_api_key=os.getenv("NEXUS_API_KEY")
        )
        assert mount["success"]

        # Read from mount
        result = nx.sandbox_run(
            sandbox["sandbox_id"],
            "bash",
            "cat /mnt/nexus/test.txt"
        )
        assert result.stdout == "Hello"

        # Cleanup
        nx.sandbox_stop(sandbox["sandbox_id"])
```

### Performance Tests

```python
# tests/performance/test_sandbox_docker_perf.py

class TestDockerPerformance:
    async def test_container_startup_time(self):
        """Container should start in <5 seconds."""
        provider = DockerSandboxProvider()

        start = time.time()
        sandbox_id = await provider.create()
        elapsed = time.time() - start

        assert elapsed < 5.0

    async def test_concurrent_execution(self):
        """Should handle 10 concurrent executions."""
        provider = DockerSandboxProvider()
        sandbox_id = await provider.create()

        tasks = [
            provider.run_code(sandbox_id, "python", f"print({i})")
            for i in range(10)
        ]

        results = await asyncio.gather(*tasks)
        assert len(results) == 10
```

## Learnings from OpenHands

Based on analysis of [OpenHands runtime implementation](https://github.com/OpenHands/OpenHands/tree/main/openhands/runtime):

### 1. Architecture Patterns

**Two-Layer Design:**
- **Provider Layer**: Container lifecycle, networking, resources
- **Execution Layer**: HTTP server inside container for complex state

**For Nexus MVP:** Use simpler direct `exec_run()` approach, not HTTP server.

### 2. Container Management

**Port Allocation:**
```python
# OpenHands uses port locking to prevent races
self._host_port, self._host_port_lock = self._find_available_port_with_lock()
```

**Health Checks with Retry:**
```python
# OpenHands uses tenacity for exponential backoff
@tenacity.retry(
    stop=tenacity.stop_after_delay(120),
    wait=tenacity.wait_exponential(multiplier=1, min=4, max=15)
)
def wait_until_alive(self):
    response = requests.get(f"{self.api_url}/health")
```

### 3. Filesystem Mounting

**Overlay Mounts (Copy-on-Write):**
```python
# OpenHands uses overlay for isolation
Mount(
    type='volume',
    driver_config=DriverConfig(
        name='local',
        options={
            'type': 'overlay',
            'o': f'lowerdir={host},upperdir={upper},workdir={work}'
        }
    )
)
```

**For Nexus:** Use FUSE mounting instead of volume mounts.

### 4. Image Building

**Docker BuildKit Integration:**
```python
# OpenHands builds from Dockerfiles with caching
buildx_cmd = [
    'docker', 'buildx', 'build',
    '--cache-from=type=local,src=/tmp/.buildx-cache',
    '--cache-to=type=local,dest=/tmp/.buildx-cache',
    f'--tag={image_name}',
    build_context_path
]
```

**For Nexus Phase 1:** Use pre-built images. Defer Dockerfile building to Phase 3.

### 5. Resource Management

**Graceful Cleanup:**
```python
# OpenHands cleanup pattern
def close(self):
    if not self.config.keep_runtime_alive:
        for container in self._list_containers():
            if container.name.startswith(self.prefix):
                container.stop()
                container.remove()
    self._release_port_locks()
```

### 6. Error Handling

**Classification of Retryable Errors:**
```python
def _is_retryable_error(exception):
    return isinstance(exception, (
        httpx.ConnectTimeout,
        httpx.NetworkError,
        ConnectionError
    ))
```

### 7. Security Best Practices

**Non-Root User:**
```dockerfile
# OpenHands Dockerfile pattern
RUN useradd -m -u 42420 openhands && \
    echo "openhands ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
USER openhands
```

**Network Isolation:**
```python
# OpenHands supports both host and bridge
network_mode='bridge'  # Isolated
# OR
network_mode='host'    # Direct localhost access
```

### 8. Key Differences for Nexus

| Aspect | OpenHands | Nexus Docker Provider |
|--------|-----------|----------------------|
| Execution | HTTP server in container | Direct `exec_run()` |
| Primary Use | AI agent runtime | Code sandbox |
| Mounting | Volume mounts | FUSE mounting |
| Complexity | High (multi-agent) | Low (single execution) |
| Image Building | BuildKit with cache | Pre-built images (Phase 1) |

## Alternatives Considered

### Alternative 1: Podman Instead of Docker

**Pros:**
- Daemonless architecture
- Rootless by default
- Better security model
- Drop-in Docker replacement

**Cons:**
- Less common in dev environments
- Compatibility issues with some images
- Additional dependency

**Decision:** Support Docker first, Podman in Phase 3 via abstraction.

### Alternative 2: HTTP Server Inside Container (OpenHands Pattern)

**Pros:**
- Better state management
- Richer API
- Easier debugging
- Connection pooling

**Cons:**
- More complex
- Slower startup
- Port management overhead
- Unnecessary for simple execution

**Decision:** Use direct `exec_run()` for MVP, revisit if needed.

### Alternative 3: Kubernetes Jobs

**Pros:**
- Better for production
- Auto-scaling
- Resource management
- High availability

**Cons:**
- Overkill for local dev
- Complex setup
- Requires cluster
- Slower startup

**Decision:** Out of scope. Use Docker for local, E2B for cloud.

### Alternative 4: VM-Based Sandboxes (Firecracker)

**Pros:**
- Better isolation
- Kernel-level security
- Fast startup
- Used by E2B/AWS Lambda

**Cons:**
- Complex setup
- Linux-only
- Requires KVM
- Not needed for dev

**Decision:** Docker provides sufficient isolation for local development.

## Open Questions

### Q1: Should we support GPU access in containers?

**Context:** Some ML workloads need GPU access.

**Options:**
- Phase 1: No GPU support
- Phase 2: Add `--gpus all` flag support
- Requires nvidia-docker runtime

**Decision:** Defer to Phase 2, add if requested.

---

### Q2: How to handle container image updates?

**Context:** Python:3.11-slim gets security updates.

**Options:**
- A: Always pull latest on create
- B: Pull only if missing
- C: User-controlled via flag

**Recommendation:** Option B (pull if missing) + `auto_pull` config option.

---

### Q3: Should we create an official `nexus-runtime` image?

**Context:** Pre-built image with nexus CLI, common tools.

**Pros:**
- Faster startup (nexus CLI pre-installed)
- Consistent environment
- Optimized for Nexus

**Cons:**
- Maintenance burden
- Image size
- Update management

**Recommendation:** Yes, create in Phase 3. Use `python:3.11-slim` for MVP.

---

### Q4: How to handle Docker daemon not running?

**Context:** `docker.from_env()` fails if daemon not running.

**Options:**
- A: Fail with clear error message
- B: Auto-start Docker (platform-specific)
- C: Fall back to E2B

**Recommendation:** Option A for MVP. Show clear error: "Docker not running. Start Docker or use --provider e2b"

---

### Q5: Should we support Docker Compose for multi-container sandboxes?

**Context:** Some apps need database + app container.

**Scope:** Complex, beyond simple code execution.

**Decision:** Out of scope for Phase 1-3. Consider for future if requested.

---

### Q6: How to handle container naming conflicts?

**Context:** Multiple Nexus instances might create containers with same names.

**Options:**
- A: Use random names (Docker default)
- B: Prefix with instance ID
- C: Use sandbox_id as name

**Recommendation:** Option B: `nexus-{instance_id}-{sandbox_id[:8]}`

---

## References

### External Resources

- [OpenHands Runtime Implementation](https://github.com/OpenHands/OpenHands/tree/main/openhands/runtime)
- [Docker SDK for Python](https://docker-py.readthedocs.io/)
- [E2B Documentation](https://e2b.dev/docs)
- [Docker Security Best Practices](https://docs.docker.com/engine/security/)

### Internal Resources

- [Issue #389: Add Docker provider](https://github.com/nexi-lab/nexus/issues/389)
- [Issue #372: Sandbox management system](https://github.com/nexi-lab/nexus/issues/372)
- [`SandboxProvider` Interface](src/nexus/core/sandbox_provider.py:37)
- [`E2BSandboxProvider` Implementation](src/nexus/core/sandbox_e2b_provider.py)
- [`SandboxManager`](src/nexus/core/sandbox_manager.py)

---

## Appendix A: Example Dockerfile

```dockerfile
# Example custom runtime Dockerfile for Nexus
# Build: docker build -f Dockerfile -t nexus-runtime:latest .
# Use: DOCKER_IMAGE=nexus-runtime:latest nexus sandbox create

FROM python:3.11-slim

# Metadata
ARG NEXUS_VERSION=0.1.0
ARG BUILD_TIME
LABEL org.nexus.version="${NEXUS_VERSION}"
LABEL org.nexus.build-time="${BUILD_TIME}"

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    fuse3 \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (optional, for JavaScript support)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user with sudo access
RUN useradd -m -u 1000 -s /bin/bash nexus && \
    echo "nexus ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Install nexus CLI (for FUSE mounting)
RUN pip install --no-cache-dir nexus-ai-fs

# Set up workspace
WORKDIR /home/nexus
RUN mkdir -p /home/nexus/workspace && \
    chown -R nexus:nexus /home/nexus

# Switch to non-root user
USER nexus

# Default command (overridden by sandbox provider)
CMD ["sleep", "infinity"]
```

Build and publish:
```bash
docker build -f Dockerfile -t nexus-runtime:latest .
docker tag nexus-runtime:latest nexus-runtime:0.1.0
docker push nexus-runtime:latest
docker push nexus-runtime:0.1.0
```

---

## Appendix B: Migration Guide from E2B

### For Developers

**Before (E2B):**
```python
from nexus import NexusFS, LocalBackend

nx = NexusFS(
    backend=LocalBackend(),
    sandbox_provider="e2b",
    e2b_api_key="e2b_xxx"
)

# Requires ngrok for local Nexus
sandbox = nx.sandbox_create("test")
nx.sandbox_connect(
    sandbox["sandbox_id"],
    nexus_url="https://xxx.ngrok.io",
    nexus_api_key="sk-xxx"
)
```

**After (Docker):**
```python
from nexus import NexusFS, LocalBackend

nx = NexusFS(
    backend=LocalBackend(),
    sandbox_provider="docker"  # That's it!
)

# Direct localhost access
sandbox = nx.sandbox_create("test")
nx.sandbox_connect(
    sandbox["sandbox_id"],
    nexus_url="http://localhost:8080",  # No ngrok needed
    nexus_api_key="sk-xxx"
)
```

### When to Use Which Provider

| Scenario | Recommended Provider |
|----------|---------------------|
| Local development | Docker |
| Local testing | Docker |
| CI/CD (with Docker) | Docker |
| Production (cloud) | E2B |
| No Docker available | E2B |
| Need GPU | E2B (Phase 1) |
| Multi-tenant SaaS | E2B |

---

**End of Design Document**
