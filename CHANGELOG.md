# Changelog

All notable changes to Nexus will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.3] - 2025-10-17

### Added

#### Metadata Export/Import (Issue #68)
- **JSONL Export Format**: Export all file metadata to human-readable JSONL
  - Sorted output for clean git diffs
  - Preserves custom key-value metadata
  - Selective export with `ExportFilter` (path prefix, time-based)
- **Flexible Import Modes**: Import metadata with conflict resolution
  - `skip`: Keep existing files (default)
  - `overwrite`: Replace with imported data
  - `remap`: Rename to avoid collisions (_imported suffix)
  - `auto`: Smart resolution (newer file wins)
- **Dry-Run Mode**: Preview import changes without modifying database
- **CLI Commands**:
  - `nexus export metadata.jsonl` - Export all metadata
  - `nexus export workspace.jsonl --prefix /workspace` - Selective export
  - `nexus import metadata.jsonl --conflict-mode=auto` - Smart import
  - `nexus import metadata.jsonl --dry-run` - Preview changes
- **Use Cases**: Git-friendly backups, zero-downtime migrations, disaster recovery

#### Batch Operations (Issue #67, #34)
- **batch_get_content_ids()**: Single-query batch retrieval of content hashes
  - Avoids N+1 query problem (1 query vs N queries)
  - ~N× performance improvement for large file sets
  - Returns `dict[path → content_hash]` mapping
- **Efficient Deduplication**: Find duplicate files in single operation
- **CLI Command**: `nexus find-duplicates` - Detect duplicate files by content
  - Shows duplicate groups with file counts
  - Calculates potential space savings
  - JSON output mode for automation

#### SQL Views for Work Detection (Issue #69)
- **5 Optimized Views** for efficient work queue operations:
  - `ready_work_items` - Files ready for processing (no blockers)
  - `pending_work_items` - Backlog of work
  - `blocked_work_items` - Dependency-blocked work (with blocker counts)
  - `in_progress_work` - Active work with worker assignment
  - `work_by_priority` - All work sorted by priority
- **O(n) Performance**: Query 10K+ work items in <100ms
- **Python API**: Metadata store methods for work queue access
  - `get_ready_work(limit=10)` - Get next batch
  - `get_pending_work()` - View backlog
  - `get_blocked_work()` - Identify bottlenecks
  - `get_in_progress_work()` - Monitor active work
  - `get_work_by_priority()` - Priority scheduling
- **CLI Command**: `nexus work ready --limit 10` - Query work queues
- **Use Cases**: Distributed task processing, DAG execution, priority scheduling

#### Type-Level Validation (Issue #37)
- **Automatic Validation**: All domain models validate before database operations
- **Clear Error Messages**: Actionable validation errors with field context
- **Fail Fast**: Catch errors before expensive DB operations
- **Validated Models**:
  - `FileMetadata` - Path, size, backend constraints
  - `FilePathModel` - Virtual path, size, tenant validation
  - `FileMetadataModel` - Key length limits, path_id checks
  - `ContentChunkModel` - SHA-256 hash format, ref_count non-negative
- **Validation Rules**:
  - Paths must start with `/` and contain no null bytes
  - Sizes and counts must be non-negative
  - Content hashes must be 64-char hex (SHA-256)
  - Metadata keys must be ≤ 255 characters

#### Resource Management (Issue #36)
- **Database Columns** added to FilePathModel:
  - `accessed_at` - Track last access time for cache eviction
  - `locked_by` - Worker/process ID for concurrent access control
- **SQL Views** for resource management:
  - `hot_tier_eviction_candidates` - Cache eviction based on access time
  - `orphaned_content_objects` - Garbage collection targets (ref_count=0)
- **Use Cases**: Hot/cold tier management, cache optimization, GC scheduling

### Fixed
- **UnboundLocalError** in embedded_demo.py:1089 (duplicate datetime import)
- **Linting Issues**: Unused variables, loop variables, function arguments
  - Renamed unused variables to `_name`, `_no_skip_existing`
  - Removed unused `old_meta`, `original_meta` variables
- **Type Checking**: All mypy errors resolved
  - Fixed `any` → `Any` type hints in views.py
  - Fixed return type annotations in test_gcs_backend.py
  - Proper type casting for list operations in CLI
- **Exception Handling**: Added `from None` to exception chains (B904)

### Changed
- **SQL Views**: Auto-created via Alembic migration `278a3d730040`
- **Import API**: Backward compatible with deprecated `overwrite` parameter
- **Export Output**: Always sorted by path for deterministic git diffs

### Documentation
- **SQL Views Guide**: Comprehensive documentation for work detection views
  - Python API examples with metadata store
  - Use cases: work queues, dependency resolution, monitoring
  - Performance benchmarks (<100ms for 10K+ items)
- **Export/Import Examples**: CLI and Python usage patterns
- **Validation Documentation**: Error messages and validation rules

### Technical Details
- **New Modules**:
  - `src/nexus/core/export_import.py` - Export/import functionality
  - `src/nexus/storage/views.py` - SQL view definitions
- **New Migrations**:
  - `278a3d730040` - Create SQL views for work detection
  - `9c0780bb05c1` - Add resource management columns
- **New Tests**:
  - `tests/unit/core/test_export_import.py` - 25+ export/import tests
  - `tests/unit/core/test_validation.py` - Domain model validation tests
  - `tests/unit/storage/test_batch_operations.py` - Batch operation tests

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

## [0.2.0] - 2025-10-19

### Added

#### FUSE Filesystem Mount (Issues #78, #79, #81, #82)
- **FUSE Integration**: Mount Nexus to local path (e.g., `/mnt/nexus`)
  - Use any standard Unix tools: `ls`, `cat`, `grep`, `vim`, `find`, etc.
  - Full read/write support with automatic metadata sync
  - Background daemon mode with `--daemon` flag
- **Mount Modes**: Three modes for different use cases
  - `smart` (default): Auto-detect file types, parse PDFs/Excel intelligently
  - `text`: Parse everything aggressively to text
  - `binary`: No parsing, return raw bytes
- **Virtual File Views**: Auto-generate `.txt` and `.md` views for binary files
  - `report.pdf.txt` - Parsed text view
  - `report.pdf.md` - Markdown view
  - Access original via `.raw/` directory
- **Auto-Parse Mode**: Binary files return text directly (grep PDFs without .txt suffix)
  - `grep "pattern" /mnt/nexus/**/*.pdf` works directly!
  - Access raw binary via `.raw/` when needed
- **All FUSE Operations**: Complete filesystem support
  - read, write, create, delete, mkdir, rmdir, rename, truncate, getattr
  - Proper Unix semantics (file handles, offsets, etc.)
  - Windows path compatibility (automatic separator conversion)
- **CLI Commands**:
  - `nexus mount /mnt/nexus` - Mount filesystem
  - `nexus mount /mnt/nexus --auto-parse --daemon` - Background auto-parse mode
  - `nexus unmount /mnt/nexus` - Unmount filesystem

#### Performance Optimizations (Issue #82)
- **Multi-Layer Caching**: TTL and LRU caches for optimal performance
  - Attribute cache (1024 entries, 60s TTL): Faster `ls` and `stat` operations
  - Content cache (100 files, LRU): Speed up repeated file reads
  - Parsed cache (50 files, LRU): Accelerate PDF/Excel text extraction
- **Automatic Cache Invalidation**: Always consistent
  - Invalidates on write, delete, rename, create operations
  - Thread-safe with RLock protection
- **Cache Metrics**: Optional performance tracking
  - Track hit/miss rates for all cache types
  - Measure cache effectiveness
- **Configurable Cache**: Tune for your workload
  - Custom cache sizes and TTL
  - Enable/disable metrics tracking
  - Python API: `cache_config` parameter in `mount_nexus()`
- **Performance**: Default settings work for most use cases
  - No configuration needed
  - Transparent performance boost

#### Content Parser Framework (Issue #80)
- **MarkItDown Integration**: Production-ready document parsing
  - PDF parser: Extract text and markdown from PDFs
  - Excel/CSV parser: Parse spreadsheets to structured data
  - Word/PowerPoint support: Extract text from Office documents
  - Extensible architecture for custom parsers
- **Document Type Detection**: Automatic MIME type detection and routing
- **Content-Aware Grep**: Search inside binary files (Issue #80)
  - Three search modes: `auto`, `parsed`, `raw`
  - `nexus grep "pattern" --file-pattern "**/*.pdf" --search-mode=parsed`
  - Results show source type: `(parsed)` or `(raw)`
  - Database-backed for fast searches
- **Parser Registry**: Automatic parser selection by file extension
  - Lazy loading for performance
  - Fallback to raw content if parsing fails

#### rclone-Style CLI Commands (Issue #81)
- **Sync Command**: One-way synchronization with hash-based change detection
  - `nexus sync ./local/dir/ /workspace/remote/`
  - `--dry-run` - Preview changes
  - `--delete` - Mirror sync (delete extra files)
  - Only copies changed files (hash comparison)
- **Copy Command**: Smart copy with automatic deduplication
  - `nexus copy ./data/ /workspace/ --recursive`
  - Skips identical files automatically
  - Leverages CAS deduplication
- **Move Command**: Efficient file/directory moves
  - `nexus move /old/path /new/path`
  - Confirmation prompts (skip with `--force`)
- **Tree Command**: Visualize directory structure
  - `nexus tree /workspace/ -L 2` - Limit depth
  - `nexus tree /workspace/ --show-size` - Show file sizes
  - ASCII tree output
- **Size Command**: Calculate directory sizes
  - `nexus size /workspace/ --human` - Human-readable (KB, MB, GB)
  - `nexus size /workspace/ --details` - Show top files
- **Features**:
  - Progress bars for long operations
  - Cross-platform paths (local ↔ Nexus)
  - Hash-based deduplication
  - Dry-run mode for all operations

#### Remote RPC Server
- **JSON-RPC Server**: Expose full NexusFileSystem API over HTTP
  - All filesystem operations: read, write, list, glob, grep, mkdir, etc.
  - JSON-RPC 2.0 protocol with proper error handling
  - Optional API key authentication (Bearer token)
- **Remote Client**: `RemoteNexusFS` for network access
  - Same API as local filesystem
  - Transparent remote operations
  - Works with all backends (local, GCS)
- **FUSE Compatible**: Mount remote Nexus servers locally
  - Network-attached Nexus filesystem
  - Access remote files as if local
- **CLI Command**: `nexus serve`
  - `nexus serve --host 0.0.0.0 --port 8080 --api-key mysecret`
  - Supports all backend types
  - Production-ready with proper logging
- **Deployment Ready**: Docker-compatible server mode
  - Persistent metadata storage
  - Backend-agnostic (GCS, local, etc.)
  - Full NFS API over network

### Fixed
- **Windows Path Separators**: Automatic conversion in `copy_recursive` (Issue #97)
- **SQLite File Locking**: Fixed Windows-specific test failures in `test_connect_functional_workflow`
- **Binary File Handling**: Proper encoding detection in grep operations

### Changed
- **Grep Output**: Now shows source type `(parsed)` or `(raw)` for transparency
- **FUSE Mount**: Default mode changed to `smart` (was `binary`)
- **Cache**: Enabled by default with sensible defaults (no config needed)

### Documentation
- **README Updates**:
  - Complete FUSE mount guide with examples
  - Performance & caching configuration
  - Remote server deployment instructions
  - rclone-style commands reference
- **Examples**:
  - `examples/fuse_mount_demo.py` - Python SDK examples with cache config
  - `examples/fuse_cli_demo.sh` - Shell script examples
- **Architecture**: Updated with cache implementation details

### Technical Details
- **New Modules**:
  - `src/nexus/fuse/cache.py` - FUSE cache manager (TTL/LRU)
  - `src/nexus/fuse/mount.py` - FUSE mount manager
  - `src/nexus/fuse/operations.py` - FUSE operations implementation
  - `src/nexus/parsers/` - Content parser framework
  - `src/nexus/server/rpc_server.py` - JSON-RPC server
  - `src/nexus/remote.py` - Remote filesystem client
  - `src/nexus/sync.py` - Sync/copy operations
- **Dependencies Added**:
  - `fusepy>=3.0.1` - FUSE Python bindings
  - `markitdown>=0.0.1a2` - Document parsing
  - `httpx>=0.27.0` - HTTP client for remote mode
  - `cachetools>=5.3.0` - LRU/TTL cache implementations
- **Tests**: 35+ unit tests for FUSE operations, 62% cache module coverage

## [0.3.0] - TBD

### Planned
- **UNIX-style file permissions** (owner, group, mode)
- **Permission operations** (chmod, chown, chgrp)
- **Access Control Lists (ACL)**
- **ReBAC (Relationship-Based Access Control)** - Zanzibar-style authorization
- **Skills System** - Anthropic-compatible SKILL.md format
- **Skill management** - create, fork, publish, search
- **Semantic skill search** - Find skills by description

---

[Unreleased]: https://github.com/nexi-lab/nexus/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/nexi-lab/nexus/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/nexi-lab/nexus/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/nexi-lab/nexus/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/nexi-lab/nexus/releases/tag/v0.1.0
