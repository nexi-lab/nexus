# Changelog

All notable changes to Nexus will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.2] - 2025-10-31

### Added

#### MCP (Model Context Protocol) Integration
- **MCP Server Implementation**: Expose Nexus filesystem via MCP protocol
  - Full MCP server in `src/nexus/mcp/server.py`
  - Supports stdio and SSE transport
  - 15+ filesystem tools exposed via MCP
  - Authentication support with API keys
- **CLI Command**: `nexus mcp serve`
  - Start MCP server for Claude Desktop integration
  - Support for both stdio and SSE transports
  - Optional API key authentication
- **Documentation**:
  - Complete MCP integration guide in `docs/integrations/mcp.md`
  - CLI reference in `docs/api/cli/mcp.md`
  - Examples with Claude Desktop configuration

#### Agent Framework Integration Examples
- **CrewAI Integration**: Complete integration example with Nexus MCP
  - Multi-agent collaboration via Nexus filesystem
  - Example in `examples/crewai/crewai_nexus_demo.py`
  - Documentation in `docs/examples/crewai.md`
- **Claude Agent SDK**: Integration example with Anthropic's Agent SDK
  - Memory-enabled agent with Nexus backend
  - Examples in `examples/claude_agent_sdk/`
  - Documentation in `docs/examples/claude-agent-sdk.md`
- **OpenAI Agents SDK**: Integration with OpenAI's Agents framework
  - ReAct agent with Nexus tools
  - Examples in `examples/openai_agents/`
  - Documentation in `docs/examples/openai-agents.md`
- **Google ADK Integration**: Integration with Google's Agent Development Kit
  - Multi-agent demo with Nexus filesystem
  - Examples in `examples/google_adk/`
  - Documentation in `docs/examples/google-adk.md`

#### Documentation Improvements
- **Complete API Documentation Navigation**: Added 30+ missing documentation pages
  - ACE (Agentic Context Engineering) subsection
  - Plugins subsection (9 pages)
  - Skills API subsection
  - All API reference pages properly linked
- **Integrations Section**: New documentation section for third-party integrations
  - LLM integration guide
  - MCP integration guide
- **Fixed Documentation URLs**: Corrected all CLI reference paths
  - Fixed paths from `cli/` to `api/cli/`
  - All documentation now properly accessible

### Fixed

#### Code Quality
- **Linting Errors**: Fixed 14 ruff linting errors across codebase
  - `examples/claude_agent_sdk/memory_agent_demo.py`: Bare except, unused arguments, nested if statements
  - `examples/crewai/crewai_nexus_demo.py`: Unused variables, contextlib.suppress usage
  - `examples/google_adk/basic_adk_agent.py`: Dict iteration, f-string formatting, unused imports/variables
  - `examples/google_adk/multi_agent_demo.py`: Python 3.11 f-string compatibility
  - `test_adk_api.py`: Import organization
- **All Ruff Checks Passing**: Zero linting errors remaining
- **Code Formatting**: All files properly formatted with ruff

#### Test Fixes
- **MarkItDownParser Test**: Fixed `test_parse_with_minimal_metadata` failure
  - Parser now defaults to `.txt` extension for files without extensions
  - Handles edge case of missing file path metadata
  - All 17 MarkItDownParser tests now passing

#### Documentation Structure
- **MkDocs Navigation**: Fixed broken documentation links
  - Corrected CLI reference paths (added `api/` prefix)
  - Added Google ADK integration to examples
  - Added missing API sections (Plugins, Skills, ACE, Integrations)
  - Removed conflicting `api/README.md` reference
- **Documentation Build**: All docs build without errors
  - Zero build errors
  - 27 API directories generated
  - All framework examples properly built

### Technical Details
- **Modified Files**:
  - `pyproject.toml` - Version bump to 0.5.2
  - `src/nexus/__init__.py` - Version bump to 0.5.2
  - `src/nexus/mcp/server.py` - MCP server implementation
  - `src/nexus/cli/commands/mcp.py` - MCP CLI commands
  - `src/nexus/parsers/markitdown_parser.py` - Handle missing file extensions
  - `mkdocs.yml` - Comprehensive navigation fixes
  - Multiple example files - Linting fixes
- **Test Coverage**: 1522 tests passing (1 previously failing test fixed)
- **Documentation Pages Added**: 30+ pages properly linked in navigation

## [0.5.1] - 2025-10-30

### Added

#### Admin API for User Management (Issue #322)
- **RESTful Admin API**: Manage users via HTTP API (no SSH access required)
  - `POST /admin/users` - Create new users with username, password, and admin flag
  - `GET /admin/users` - List all users with detailed information
  - `GET /admin/users/{username}` - Get specific user details
  - `DELETE /admin/users/{username}` - Delete user accounts
  - `PUT /admin/users/{username}/password` - Update user passwords
  - `PUT /admin/users/{username}/admin` - Toggle admin privileges
- **CLI Commands**: Manage users from command line
  - `nexus admin create-user <username>` - Interactive user creation
  - `nexus admin list-users` - Display all users in table format
  - `nexus admin delete-user <username>` - Remove user with confirmation
  - `nexus admin reset-password <username>` - Reset user password
  - `nexus admin grant-admin <username>` - Grant admin privileges
  - `nexus admin revoke-admin <username>` - Revoke admin privileges
- **Authentication**: Bearer token auth for admin endpoints
  - Requires admin user credentials
  - JWT-based authentication via AuthManager
- **Documentation**: Complete API documentation with examples
  - HTTP API reference in `docs/api/rpc-api.md`
  - CLI usage guide
  - Security best practices

#### Workspace Management API Documentation
- **Complete RPC API Docs**: Added comprehensive workspace management documentation
  - `register_workspace` - Register directories as workspaces
  - `list_workspaces` - List all registered workspaces
  - `get_workspace_info` - Get workspace information
  - `unregister_workspace` - Remove workspace registration
  - `workspace_snapshot` - Create workspace snapshots
  - `workspace_log` - List workspace snapshots
  - `workspace_restore` - Restore workspace to snapshot
  - `workspace_diff` - Compare snapshots
- **JSON-RPC Examples**: Complete request/response examples for all operations

### Fixed

#### ReBAC Permission Path Normalization (Issue #330)
- **Path Format Consistency**: Fixed permission check failures due to path format mismatches
  - Router uses relative paths: `workspace/bob`
  - ReBAC tuples use absolute paths: `/workspace/bob`
  - Now normalizes paths by adding leading slash when missing
- **Permission Checks**: Users with correct ownership tuples can now access their workspaces
  - Fixed write permission denials for valid users
  - Consistent path format across permission layers
- **Test Coverage**: Added comprehensive path normalization tests
  - 23 permission enforcer tests all passing
  - Integration tests verify cross-user isolation

#### PostgreSQL Cursor Handling (Issue #323)
- **Unified Row Access**: Refactored cursor handling for consistent dict-like row access
  - Use `RealDictCursor` for PostgreSQL
  - Use `sqlite3.Row` for SQLite
  - Removed 25+ conditional row access patterns
- **Server Startup Fix**: Resolved `AssertionError: Could not determine version from string 'version'`
  - Cursor factory set per-cursor, not per-connection
  - Prevents breaking SQLAlchemy's internal version detection
- **Code Quality**: Simplified codebase with single unified code path
  - Removed `hasattr(row, "keys")` conditionals
  - Refactored 37 cursor creation points
  - Improved maintainability

### Changed

#### RPC Server Improvements
- **Enhanced Auth Logging**: Better startup logging for authentication methods
  - Shows database provider class name
  - Distinguishes between static API key and no auth
  - Improved debugging experience
- **Documentation**: Added 298 lines of workspace API documentation

### Technical Details
- **New API Endpoints**: 6 new admin endpoints for user management
- **New CLI Commands**: 6 new `nexus admin` commands
- **Database Changes**: No schema migrations required
- **Test Coverage**: All tests passing (17/17 ReBAC, 44/44 metadata store)
- **Files Modified**:
  - `src/nexus/server/rpc_server.py` - Admin API endpoints, auth logging
  - `src/nexus/cli/admin.py` - Admin CLI commands
  - `src/nexus/core/permissions.py` - Path normalization
  - `src/nexus/core/permissions_enhanced.py` - Path normalization
  - `src/nexus/core/rebac_manager.py` - Cursor handling
  - `docs/api/rpc-api.md` - Workspace API documentation

## [0.5.0] - 2025-10-29

### Added

#### Database Schema + Agent Identity System
- **Enhanced Database Schema**: Improved database structure for better scalability
  - Optimized indexes for faster queries
  - Better support for multi-tenant operations
  - Schema migrations for production deployments

#### ReBAC (Relationship-Based Access Control) Enhancements
- **Complete ReBAC Implementation**: Full Zanzibar-style authorization system
  - Enhanced tuple storage and querying
  - Improved relationship resolution
  - Better tenant isolation in ReBAC checks
  - Authentication support with OIDC and local auth
  - `authlib` dependency for OAuth/OIDC integration
  - `bcrypt` for secure password hashing

#### Test Infrastructure Improvements
- **Reorganized Test Suite**: Better test organization and maintainability
  - Tests reorganized into logical subdirectories
  - Improved test isolation for parallel execution
  - Comprehensive test coverage for workflows module
  - Fixed flaky macOS RPC server tests

### Fixed
- **RPC Parity Test**: Fixed test path in GitHub Actions workflow
- **Linting and Type Errors**: Resolved all linting and type checking issues
- **Test Fixtures**: Improved tenant isolation and session management in tests
- **Permission System**: Updated permission check expectations in workspace manager

### Changed
- **Coverage Threshold**: Lowered to 60% for v0.5.0 to accommodate new features
- **Test Organization**: Tests now organized in subdirectories for better maintainability

## [0.3.9] - 2025-10-23

### Added

#### Time-Travel Debugging (Issue #186)
- **Read Files at Historical Points**: Query filesystem state at any operation point
  - `nexus cat /file.txt --at-operation <op_id>` - Read file at historical operation
  - `nexus ls /workspace --at-operation <op_id>` - List directory at historical point
  - `nexus ops diff /file.txt <op1> <op2> --show-content` - Full unified diff between operations
- **Non-Destructive History Exploration**: Debug without modifying current state
  - Query file content at any past operation
  - Compare states between operations
  - Understand agent behavior over time
- **Full Content Access**: Returns complete file content and metadata
  - CAS-backed content retrieval (zero storage overhead)
  - Metadata reconstruction from operation snapshots
  - Unified diff support (like `git diff`)
- **Python SDK**: TimeTravelReader class for programmatic access
  - `get_file_at_operation(path, operation_id)` - Read historical file
  - `list_files_at_operation(path, operation_id)` - List historical directory
  - `diff_operations(path, op1, op2)` - Compare file states
- **Use Cases**:
  - "What was the file content 10 operations ago?"
  - Track agent modifications over time
  - Post-mortem analysis without undo
  - Concurrent agent debugging and analysis
- **CLI Command**: `nexus ops diff`
  - Metadata diff (default): Shows size changes, timestamps
  - Content diff (`--show-content`): Full unified diff with line-by-line changes
- **Tests**: 9 comprehensive unit tests with 83% coverage

#### Operation Log - Undo & Audit Trail (Issue #185)
- **Automatic Operation Logging**: Track all filesystem operations
  - Write, delete, and rename operations logged automatically
  - CAS-backed snapshots of previous content (zero storage overhead)
  - Complete audit trail with timestamps
- **Undo Capability**: Reverse any operation
  - `nexus undo` - Undo last operation (with confirmation)
  - `nexus undo --agent my-agent` - Undo agent-specific operation
  - `nexus undo --yes` - Skip confirmation prompt
- **Undo Behavior by Operation Type**:
  - Write (new file): Deletes the newly created file
  - Write (update): Restores previous version from CAS snapshot
  - Delete: Restores file content and metadata from CAS
  - Rename: Renames file back to original path
- **Filtered Queries**: Search operations by multiple criteria
  - `nexus ops log --agent my-agent` - Filter by agent
  - `nexus ops log --type write` - Filter by operation type
  - `nexus ops log --path /workspace/file.txt` - Filter by path
  - `nexus ops log --status failure` - Show failed operations
- **Python SDK**: OperationLogger class
  - `list_operations(agent_id, operation_type, path, status)` - Query operations
  - `get_last_operation(agent_id)` - Get most recent operation
  - `get_path_history(path)` - Get all operations for a path
- **Multi-Agent Safe**: Track operations per agent for team workflows
- **Database Schema**: OperationLogModel with indexed queries
  - operation_id, operation_type, path, agent_id, tenant_id
  - snapshot_hash (previous content), metadata_snapshot (previous metadata)
  - created_at timestamp, status (success/failure)
- **Tests**: 12 comprehensive unit tests

#### Workspace Versioning - Time-Travel for Agent Workspaces (Issue #183)
- **Snapshot & Restore**: Save and restore entire workspace state
  - Create point-in-time snapshots of all files
  - Restore workspace to any previous snapshot
  - Zero storage overhead (snapshots use CAS manifest files)
- **Workspace Operations**:
  - `create_snapshot(label)` - Create named snapshot
  - `list_snapshots()` - View all snapshots
  - `restore_snapshot(snapshot_id)` - Restore to snapshot
  - `delete_snapshot(snapshot_id)` - Delete snapshot
- **CLI Commands**:
  - `nexus workspace snapshot --label "before-refactor"` - Create snapshot
  - `nexus workspace list` - List all snapshots
  - `nexus workspace restore <snapshot_id>` - Restore snapshot
  - `nexus workspace delete <snapshot_id>` - Delete snapshot
- **Manifest-Based Snapshots**: JSON manifests in CAS
  - Lists all files and their content hashes
  - Stored in CAS (deduplicated)
  - Fast restoration (metadata updates only)
- **Use Cases**:
  - Safe experimentation ("snapshot before risky change")
  - Checkpointing during long workflows
  - Agent state recovery
  - Rollback destructive operations

#### Performance Optimizations

**Content Caching (Issue #211)** - 10x faster reads
- **LRU Content Cache**: Cache file content in memory
  - Default 100 files, configurable size
  - Automatic cache invalidation on writes
  - Thread-safe with proper locking
- **Performance**: 10x speedup for repeated reads
  - Hot path optimization for AI workloads
  - Transparent caching (no code changes needed)
- **Configuration**:
  ```python
  nx = nexus.connect(config={"content_cache_size": 200})
  ```

**Batch Write API (Issue #212)** - 13x faster operations
- **Bulk Upload**: Write multiple files in single transaction
  - `write_batch([(path, content), ...])` - Batch write
  - Single database transaction for all files
  - Reduced overhead per file
- **Performance**: 13x faster for small files
  - 1000 small files: ~13 seconds → ~1 second
  - Optimal for AI checkpoints and logs
- **Use Cases**:
  - Checkpoint uploads
  - Log file batching
  - Dataset imports
- **Example**:
  ```python
  files = [(f"/logs/log_{i}.txt", f"Log {i}") for i in range(1000)]
  nx.write_batch(files)  # 13x faster than individual writes
  ```

**Performance Benchmarking Suite (Issue #196)**
- **Comprehensive Benchmarks**: Measure all core operations
  - Read, write, list, glob, grep benchmarks
  - Batch operations performance tests
  - Cache effectiveness metrics
- **CLI Command**: `nexus benchmark`
  - Automatic performance testing
  - Comparison with/without cache
  - HTML report generation

### Fixed
- **Windows Concurrency**: Fixed file locking issues in LocalBackend
  - Proper file handle management
  - Windows-specific test stability
- **CAS File Not Found**: Improved error messages for missing content
  - Better error context and debugging info
  - Graceful handling of edge cases
- **Skills Tests**: Fixed PostgreSQL compatibility issues
  - Proper workspace cleanup
  - Database isolation in tests
- **Remote Client**: Enhanced error handling
  - Better network error messages
  - Connection timeout handling
- **Import Scope**: Fixed `NexusFS` variable scope in CLI commands
  - Moved imports to function level for `ls --long` compatibility
  - Proper error handling for import failures

### Changed
- **CLI Structure**: Reorganized commands for better UX
  - Grouped related commands
  - Consistent naming conventions
  - Improved help text
- **Operation Log**: Now required for workspace versioning
  - Automatic integration
  - Shared infrastructure

### Documentation
- **Time-Travel Guide**: Complete documentation with examples
  - CLI usage examples
  - Python SDK guide
  - Use cases and best practices
  - Demo scripts (Python + Shell)
- **Operation Log Guide**: Comprehensive docs
  - Undo behavior by operation type
  - Query filtering examples
  - Multi-agent workflows
- **Workspace Versioning Guide**: Snapshot/restore documentation
  - Snapshot creation and management
  - Restoration procedures
  - CAS manifest format
- **Performance Guide**: Optimization best practices
  - Cache configuration
  - Batch write patterns
  - Benchmarking results

### Technical Details
- **New Modules**:
  - `src/nexus/storage/time_travel.py` - Time-travel debugging (118 lines)
  - `src/nexus/storage/operation_logger.py` - Operation logging (194 lines)
  - `src/nexus/core/workspace_manager.py` - Workspace snapshots
- **New Database Tables**:
  - `operation_log` - Operation audit trail
  - `workspace_snapshots` - Workspace snapshot metadata
- **New Alembic Migrations**:
  - `add_operation_log` - Create operation_log table
  - `add_workspace_snapshots` - Create workspace_snapshots table
- **New Tests**: 21+ comprehensive tests
  - Time-travel debugging: 9 tests (83% coverage)
  - Operation log: 12 tests
  - Workspace versioning: Tests included
- **New Examples**:
  - `examples/py_demo/time_travel_demo.py` - Python time-travel demo
  - `examples/script_demo/time_travel_demo.sh` - Shell time-travel demo
  - `examples/py_demo/operation_log_demo.py` - Operation log demo
  - `examples/script_demo/operation_log_demo.sh` - Operation log shell demo

### Performance Metrics
- **Batch Write**: 13x faster for 1000 small files (13s → 1s)
- **Content Cache**: 10x faster for repeated reads
- **Time-Travel**: <100ms for historical file reads
- **Operation Log**: Minimal overhead (<5% on write operations)

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

## [0.3.0] - 2025-10-22

### Added

#### UNIX-Style File Permissions & Security (Issue #84)
- **File Ownership**: Full UNIX-style permission model
  - Owner, group, and mode (e.g., 0o644, 0o755)
  - Stored in metadata for every file and directory
  - Enforced on all file operations (read, write, delete)
- **Permission Operations**:
  - `chmod`: Change file permissions (numeric or symbolic modes)
  - `chown`: Change file owner
  - `chgrp`: Change file group
  - CLI: `nexus chmod 755 /file.txt`, `nexus chown alice /file.txt`
- **Access Control Lists (ACL)**: Fine-grained permission control
  - User-level and group-level ACLs
  - Grant/deny READ, WRITE, EXECUTE permissions
  - Per-file ACL rules stored in database
  - CLI: `nexus acl grant alice READ /file.txt`
- **Multi-Layer Permission Checks**: Defense in depth
  1. UNIX permissions (owner/group/other)
  2. ACL rules (user/group specific)
  3. ReBAC relationships (Zanzibar-style)
- **Permission Contexts**: Flexible operation contexts
  - `OperationContext(user, groups, is_admin, is_system)`
  - Per-operation permission override
  - Default context from NexusFS init

#### Permission Inheritance (Issue #111)
- **Automatic Inheritance**: New files inherit parent directory permissions
  - Owner, group, and mode propagated from parent
  - Directories: Inherit parent mode with execute bits added
  - Files: Inherit parent mode with execute bits removed
- **Smart Mode Calculation**:
  - Directories: Parent mode | 0o111 (add execute)
  - Files: Parent mode & ~0o111 (remove execute)
- **Example**: Parent dir (0o755) → Child file (0o644), Child dir (0o755)

#### Default Permission Policies (Issue #110)
- **Namespace-Level Policies**: Configure default permissions per namespace
  - Match paths with glob patterns (e.g., `/workspace/*/private/*`)
  - Set owner, group, mode with variable substitution
  - Variables: `{tenant_id}`, `{agent_id}`, `{user_id}`
- **Policy Priority**: Most specific pattern wins
  - Longer patterns override shorter ones
  - Explicit policies override defaults
- **Example Policy**:
  ```yaml
  - path: "/workspace/{tenant_id}/{agent_id}/*"
    owner: "{agent_id}"
    group: "{tenant_id}"
    mode: 0o644
  ```
- **CLI Command**: `nexus policy list`, `nexus policy create`

#### ReBAC (Relationship-Based Access Control) (Issue #91)
- **Zanzibar-Style Authorization**: Google-inspired fine-grained permissions
  - Tuples: `(subject, relation, object)` format
  - Example: `(alice, editor, doc1)`, `(doc1, parent, folder1)`
  - Transitive relationships with configurable depth limits
- **Relationship Types**:
  - Direct relations: `owner`, `editor`, `viewer`
  - Indirect relations: `parent`, `member`
  - Permission resolution with graph traversal
- **Database-Backed**: Efficient tuple storage and querying
  - SQLite/PostgreSQL compatible
  - Indexed for fast lookups
  - TTL-based caching for performance
- **API**:
  - `check_permission(user, permission, path)` - Check access
  - `add_tuple(subject, relation, object)` - Grant permission
  - `remove_tuple(subject, relation, object)` - Revoke permission

#### Skills System - Core Features (Issue #85)
- **Anthropic-Compatible Format**: SKILL.md file format support
  - Frontmatter: YAML metadata (name, version, description, tags)
  - Instructions: Markdown content for AI agents
  - Validation: Schema validation for metadata
- **Skill Registry**: Discover and load skills from filesystem
  - Auto-discovery from `/workspace/.nexus/skills/`
  - Lazy loading for performance
  - Caching for fast lookups
- **Skill Parser**: Parse SKILL.md files
  - Extract frontmatter (YAML)
  - Parse markdown content
  - Validate schema and dependencies
- **Version Management**: Semantic versioning support
  - Version comparison (1.0.0 > 0.9.0)
  - Dependency resolution
  - Upgrade notifications
- **CLI Commands**:
  - `nexus skills list` - Show all skills
  - `nexus skills info <skill>` - Show skill details
  - `nexus skills create <name>` - Create new skill from template

#### Skills System - Management Features (Issue #86)
- **Skill Creation**: Template-based skill generation
  - Interactive prompts for metadata
  - Pre-filled templates with examples
  - Auto-generate SKILL.md with proper structure
- **Skill Forking**: Copy and modify existing skills
  - `nexus skills fork <source> <dest>`
  - Preserves metadata and instructions
  - Updates version and authorship
- **Skill Export**: Export skills for sharing
  - Export to standalone directory
  - Include dependencies
  - Tarball support for distribution
- **Skill Import**: Import skills from external sources
  - `nexus skills import <path>`
  - Validate before import
  - Conflict resolution

#### Skills System - Enterprise Features (Issue #87)
- **Skill Analytics**: Track skill usage and performance
  - Execution count, success rate, error tracking
  - Time-series metrics
  - Usage trends over time
- **Skill Governance**: Policy enforcement for skills
  - Required tags (e.g., "approved", "production")
  - Owner approval workflows
  - Deprecation warnings
- **Skill Audit**: Compliance and security auditing
  - Track all skill modifications
  - Who created/modified/executed skills
  - Audit log with timestamps
- **Skill Search**: Find skills by description/tags
  - Full-text search in descriptions
  - Tag-based filtering
  - Relevance ranking

#### Version Tracking (Issue #88)
- **File Version History**: Track all versions of files
  - Automatic versioning on every write
  - Version number increments automatically
  - Efficient storage using CAS (no duplication)
- **Version Operations**:
  - `get_version(path, version)` - Retrieve specific version
  - `list_versions(path)` - Show version history
  - `rollback(path, version)` - Restore old version
  - `diff_versions(path, v1, v2)` - Compare versions
- **CLI Commands**:
  - `nexus versions <path>` - List all versions
  - `nexus cat <path> --version 2` - Read specific version
  - `nexus rollback <path> 2` - Restore to version 2
  - `nexus diff <path> --v1 1 --v2 3` - Compare versions
- **Metadata-Only**: No content duplication
  - Versions reference CAS hashes
  - Space-efficient storage
  - Fast version switching

#### Database Support
- **PostgreSQL Support**: Production-ready relational database
  - All features work with PostgreSQL
  - Connection pooling and optimization
  - Production deployment ready
  - Environment variable: `NEXUS_DATABASE_URL`
- **SQLite Compatibility**: Still fully supported
  - Embedded mode for local development
  - File-based storage
  - No server required

#### FUSE Improvements
- **Metadata-Only Rename**: Instant file/directory moves
  - No content copying (uses metadata update)
  - Works for any file size
  - Atomic operation
- **Directory Move Support**: Move entire directories
  - Recursive file renaming in metadata
  - No content I/O
  - Fast directory restructuring
- **Virtual Views for Remote**: `.txt` and `.md` views work over network
  - Remote mounts support parsed views
  - Consistent behavior with local mounts
  - Fix for issue #172
- **Enhanced mv Behavior**: Proper Unix semantics
  - Error if destination exists
  - Create destination if it doesn't exist
  - Confirmation prompts

#### Plugin System (Issue #168)
- **Extensibility Framework**: Hook-based plugin architecture
  - Pre/post hooks for file operations
  - Event-driven design
  - Plugin discovery and loading
- **Plugin API**:
  - `on_read`, `on_write`, `on_delete` hooks
  - Context access for all operations
  - Error handling and logging
- **Use Cases**:
  - Custom validation logic
  - Audit logging
  - External integrations
  - Custom transformations

### Fixed
- **Tenant Isolation**: Fixed delete operation to check tenant/agent isolation before permission checks
  - Ensures `AccessDeniedError` is raised before `PermissionError`
  - Fixes tests in `test_embedded_tenant_isolation.py`
  - Reordered checks in `NexusFS.delete()` method
- **FUSE Getattr Operations**: Added metadata mock to fix FUSE tests
  - Fixed `FuseOSError: [Errno 5]` in getattr operations
  - Added `metadata` attribute to mock filesystem
  - Fixes 4 tests in `test_fuse_operations.py`
- **Exception Hierarchy**: Made `NexusFileNotFoundError` inherit from `FileNotFoundError`
  - Fixes permission operation tests expecting `FileNotFoundError`
  - Proper exception hierarchy for Python compatibility
  - Fixes 3 tests in `test_permission_operations.py`
- **FUSE Mount Daemon Mode**: Fixed background daemon startup
- **Windows Path Separators**: Proper handling in copy operations
- **Virtual Views**: Fixed `.txt`/`.md` view generation for remote mounts

### Changed
- **Permission Enforcement**: Opt-in by default
  - Set `enforce_permissions=True` in NexusFS init
  - Backward compatible (disabled by default)
  - Gradual migration path
- **Delete Operation**: Reordered permission checks
  - Router check (tenant isolation) before permission check
  - Consistent error priority
- **Test Coverage**: Improved from 29% to 40% for core modules

### Documentation
- **Permissions Implementation Guide**: Comprehensive guide to permission system
  - Architecture overview
  - Permission layers explained
  - Usage examples and best practices
- **Skills System Guide**: Complete documentation
  - SKILL.md format specification
  - Skill lifecycle management
  - Enterprise features guide
- **Deployment Guide**: Production deployment instructions
  - PostgreSQL setup
  - GCP Cloud Run deployment
  - Docker configuration
  - Environment variables

### Technical Details
- **New Modules**:
  - `src/nexus/core/permissions.py` - Permission system core
  - `src/nexus/core/acl.py` - ACL implementation
  - `src/nexus/core/rebac.py` - ReBAC tuples and checks
  - `src/nexus/core/rebac_manager.py` - ReBAC manager
  - `src/nexus/core/permission_policy.py` - Policy matcher
  - `src/nexus/skills/` - Complete skills system
  - `src/nexus/plugins/` - Plugin framework
- **New Database Tables**:
  - `acl_entries` - ACL rules
  - `rebac_tuples` - ReBAC relationships
  - `permission_policies` - Default policies
  - `skill_metrics` - Skill analytics
  - `skill_audit_log` - Skill audit trail
- **New Alembic Migrations**:
  - `c0ff28ce` - Add UNIX permissions and ACL support
- **New Tests**: 85+ new tests
  - Permission system: 37 tests
  - ACL: 24 tests
  - Skills: 42 tests
  - Tenant isolation: 17 tests
- **Coverage**: Increased from 29% to 40% overall

---

[Unreleased]: https://github.com/nexi-lab/nexus/compare/v0.5.2...HEAD
[0.5.2]: https://github.com/nexi-lab/nexus/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/nexi-lab/nexus/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/nexi-lab/nexus/compare/v0.3.9...v0.5.0
[0.3.9]: https://github.com/nexi-lab/nexus/compare/v0.3.0...v0.3.9
[0.3.0]: https://github.com/nexi-lab/nexus/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/nexi-lab/nexus/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/nexi-lab/nexus/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/nexi-lab/nexus/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/nexi-lab/nexus/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/nexi-lab/nexus/releases/tag/v0.1.0
