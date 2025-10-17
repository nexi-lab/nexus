# Changelog

All notable changes to Nexus will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2025-10-17

### Added

#### Core Filesystem
- **Embedded Mode**: Zero-deployment, library-mode filesystem (like SQLite)
- **File Operations**: Complete read/write/delete operations with metadata tracking
- **Virtual Path Routing**: Map virtual paths to physical backend locations
- **Content-Addressable Storage (CAS)**: Automatic deduplication with 30-50% storage savings
- **Reference Counting**: Safe deletion with automatic garbage collection

#### Database & Storage
- **SQLite Metadata Store**: Production-ready metadata storage with SQLAlchemy ORM
- **Alembic Migrations**: Database schema versioning and migration support
- **Local Filesystem Backend**: High-performance local storage backend
- **File Metadata**: Track size, etag, created_at, modified_at, mime_type
- **Custom Metadata**: Store arbitrary key-value metadata per file

#### Directory Operations
- **mkdir**: Create directories with `--parents` support
- **rmdir**: Remove directories with `--recursive` support
- **is_directory**: Check if path is a directory
- **Automatic Directory Creation**: Parent directories created on file write

#### File Discovery (Issue #6)
- **Enhanced list()**: List files with `--recursive` and `--details` options
- **glob()**: Pattern matching with `*`, `**`, `?`, `[...]` support
  - `nexus glob "**/*.py"` - Find all Python files recursively
  - `nexus glob "test_*.py"` - Find test files
- **grep()**: Regex search in file contents with filtering
  - Case-insensitive search
  - File pattern filtering
  - Result limiting
  - Automatic binary file skipping

#### CLI Interface (Issue #13)
- **Beautiful CLI**: Click framework with Rich for colored output
- **12 Commands**: init, ls, cat, write, cp, rm, glob, grep, mkdir, rmdir, info, version
- **Syntax Highlighting**: Python, JSON, Markdown syntax highlighting in `cat`
- **Rich Tables**: Detailed file listings with formatted tables
- **Global Options**:
  - `--config`: Point to custom config file
  - `--data-dir`: Override data directory
- **Interactive Prompts**: Confirmations for delete operations

#### Multi-Tenancy & Isolation
- **Tenant Isolation**: Workspace isolation by tenant_id
- **Agent Isolation**: Agent-specific workspaces within tenants
- **Admin Mode**: Bypass isolation for administrative tasks
- **Namespace System**: workspace/, shared/, external/, system/, archives/
  - Read-only namespaces (archives, system)
  - Admin-only namespaces (system)
  - Tenant-scoped namespaces

#### Configuration
- **Multiple Config Sources**: YAML files, environment variables, Python dicts
- **Auto-Discovery**: Automatic config file discovery (./nexus.yaml, ~/.nexus/config.yaml)
- **Environment Variables**: Full support for NEXUS_* env vars
- **Flexible Configuration**: Configure mode, data_dir, cache, tenancy, and more

#### Path Router
- **Virtual Path Mapping**: Abstract file paths from physical storage
- **Multi-Mount Support**: Different paths can map to different backends
- **Longest-Prefix Matching**: Intelligent routing to appropriate backend
- **Path Validation**: Security checks (null bytes, path traversal, control chars)
- **Backend Abstraction**: Clean interface for future S3/GDrive/SharePoint support

#### Abstract Interface
- **NexusFilesystem**: Abstract base class for all modes
- **Consistent API**: Same interface across Embedded/Monolith/Distributed
- **Type Safety**: Full type hints and mypy compliance
- **Context Manager**: Proper resource cleanup with `with` statement

### Documentation
- **Comprehensive README**: Installation, usage, configuration examples
- **CLI Documentation**: Help text for all commands with examples
- **API Examples**: Python usage examples for all operations
- **Configuration Guide**: Complete config options and examples
- **Architecture Docs**: Explanation of design and components

### Testing
- **Unit Tests**: 41+ unit tests for embedded mode
- **Integration Tests**: Metadata store integration tests
- **CLI Test Script**: Automated CLI testing with 30+ test cases
- **High Coverage**: 85% code coverage on core modules

### Infrastructure
- **GitHub Actions**: Automated testing, linting, and releases
- **Pre-commit Hooks**: Automatic code formatting and linting
- **Type Checking**: Full mypy type checking
- **Code Quality**: Ruff for linting and formatting

### Fixed
- Backward compatibility in `list()` method with deprecated `prefix` parameter
- Type annotation conflicts between method names and built-in types
- CLI function name collisions after mass replacements

### Technical Details
- **Python**: 3.11+ required
- **Database**: SQLite (embedded), PostgreSQL (future)
- **Storage**: Local filesystem, S3/GDrive (future)
- **CLI**: Click 8.1+, Rich 13.7+
- **ORM**: SQLAlchemy 2.0+
- **Migrations**: Alembic 1.13+

## [0.2.0] - TBD

### Planned
- UNIX-style file permissions (owner, group, mode)
- Permission operations (chmod, chown, chgrp)
- Access Control Lists (ACL)
- Permission inheritance and policies

## [0.3.0] - TBD

### Planned
- Monolithic server mode
- REST API with FastAPI
- Multi-tenancy with authentication
- Docker deployment

---

[Unreleased]: https://github.com/nexi-lab/nexus/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nexi-lab/nexus/releases/tag/v0.1.0
