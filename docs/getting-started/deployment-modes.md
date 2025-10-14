# Deployment Modes

**Note: v0.1.0 only implements embedded mode. Monolithic and distributed modes are planned for future releases.**

## Current Support (v0.1.0)

### ✅ Embedded Mode - Available Now

Embedded mode runs entirely in-process with no external dependencies. Perfect for:

- Individual developers
- CLI tools
- Testing and development
- Desktop applications
- Single-user scenarios

#### Architecture

```
┌─────────────────┐
│  Your Application │
│                 │
│  ┌───────────┐ │
│  │   Nexus   │ │  SQLite
│  │  Embedded │ │  Local FS
│  └───────────┘ │
└─────────────────┘
```

#### Setup

```python
import nexus

# Zero configuration - just works
nx = nexus.connect()

# Or with config file
nx = nexus.connect(config_file="nexus.yaml")
```

#### Configuration

```yaml
mode: embedded
data_dir: ./nexus-data
```

#### Features

- ✅ File operations (read, write, delete, list)
- ✅ SQLite metadata store
- ✅ Local filesystem backend
- ✅ Virtual path management
- ✅ Metadata tracking (size, etag, timestamps)
- ❌ Multi-user support (single process only)
- ❌ Remote access
- ❌ High availability

## Planned Modes (Future Releases)

### 🚧 Monolithic Mode - Planned for v0.5.0

Single server deployment for small teams.

**Planned Features:**
- Multi-user support (1-20 users)
- REST API
- API key authentication
- PostgreSQL metadata store
- Redis caching
- Docker deployment

**Status:** Not yet implemented

### 🚧 Distributed Mode - Planned for v0.9.0

Fully distributed architecture for enterprise scale.

**Planned Features:**
- Horizontal scaling
- High availability
- Load balancing
- Multi-region support
- Kubernetes deployment
- Advanced monitoring

**Status:** Not yet implemented

## Migration Path

When monolithic and distributed modes are implemented, migration will be supported:

```bash
# Export from embedded (future)
nexus export --format jsonl > metadata.jsonl

# Import to monolithic (future)
nexus import --url http://server:8080 < metadata.jsonl
```

## Current Limitations

Since v0.1.0 only supports embedded mode:

- **Single process only** - No concurrent access from multiple processes
- **No remote access** - Must run in same process as application
- **No multi-user** - Designed for single-user scenarios
- **Local only** - Files stored on local filesystem only

## When to Use Embedded Mode

**Good fit:**
- ✅ Personal projects
- ✅ CLI tools
- ✅ Desktop applications
- ✅ Development and testing
- ✅ Single-user workflows
- ✅ Prototyping

**Not recommended:**
- ❌ Multi-user applications (wait for v0.5.0)
- ❌ Web services with concurrent users (wait for v0.5.0)
- ❌ Enterprise deployments (wait for v0.9.0)
- ❌ High-availability requirements (wait for v0.9.0)

## Roadmap

See the [main README](../index.md#roadmap) for the full feature roadmap and timeline.

## Next Steps

- [Quick Start](quickstart.md) - Get started with embedded mode
- [Configuration](configuration.md) - Configure embedded mode
- [API Reference](../api/api.md) - Explore the API
