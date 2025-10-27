# Version Compatibility and Migration

← [API Documentation](README.md)

This document describes version compatibility, migration guides, and additional resources.

### v0.3.9 (Current)

**Supported:**
- ✅ Embedded mode
- ✅ SQLite backend
- ✅ PostgreSQL backend
- ✅ Local filesystem storage
- ✅ Google Cloud Storage (GCS) backend
- ✅ Automatic metadata tracking
- ✅ Version tracking with CAS
- ✅ Content deduplication
- ✅ File discovery (list, glob, grep)
- ✅ Directory operations (mkdir, rmdir)
- ✅ Workspace snapshots
- ✅ Document parsing (PDF, Excel, PowerPoint, etc.)
- ✅ ReBAC permissions
- ✅ Multi-tenant isolation
- ✅ Operation contexts
- ✅ In-memory metadata caching
- ✅ Batch operations
- ✅ Optimistic concurrency control

**Not Yet Implemented:**
- ⏳ Monolithic mode (v0.4.0+)
- ⏳ Distributed mode (v0.5.0+)
- ⏳ S3/Azure backends (v0.4.0+)
- ⏳ Semantic search (v0.4.0+)
- ⏳ Skills system (v0.3.0+ - in progress)

### API Stability

The API is **stable** for v0.3.x embedded mode. Breaking changes will be avoided in minor versions.

---
## Further Reading

### Documentation

- **[Documentation Index](../DOCUMENTATION_INDEX.md)**: Complete documentation overview
- **[Getting Started Guide](../guides/)**: Step-by-step tutorials
- **[Authentication Overview](../guides/authentication-overview.md)**: Authentication and security
- **[Multi-Backend Guide](../multi-backend.md)**: Using different storage backends
- **[Permission System](../PERMISSION_SYSTEM.md)**: ReBAC and permissions

### Examples

- **Python Demos**: `examples/py_demo/`
  - `embedded_demo.py`: Comprehensive embedded mode demo
  - `multi_backend_demo.py`: Multi-backend examples
  - `workspace_demo.py`: Workspace snapshot examples
  - `CORRECT_API_USAGE.py`: Best practices for using contexts
- **Shell Scripts**: `examples/script_demo/`
  - `backend_switching_demo.sh`: Switching between backends
  - `pipeline_demo.sh`: Building data pipelines
  - `time_travel_demo.sh`: Version tracking examples

### Development

- **[Development Guide](../development.md)**: Contributing to Nexus
- **[Architecture](../design/)**: System architecture documents
- **[RPC Parity Guide](../RPC_PARITY_GUIDE.md)**: Embedded-server parity

---

## Support

- **GitHub Issues**: https://github.com/nexi-lab/nexus/issues
- **Documentation**: `docs/`
- **Examples**: `examples/`
