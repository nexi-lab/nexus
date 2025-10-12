#!/bin/bash
set -e

# Script to create GitHub issues for Nexus v0.1.0 and v0.2.0
# Based on NEXUS_COMPREHENSIVE_ARCHITECTURE.md
# Requires: gh (GitHub CLI) to be installed and authenticated
# Repository must exist on GitHub first

REPO="nexi-lab/nexus"

echo "Creating GitHub issues for Nexus v0.1.0 and v0.2.0"
echo "Repository: $REPO"
echo ""

# Check if gh is installed
if ! command -v gh &> /dev/null; then
    echo "Error: GitHub CLI (gh) is not installed."
    echo "Install it from: https://cli.github.com/"
    exit 1
fi

# Check if authenticated
if ! gh auth status &> /dev/null; then
    echo "Error: Not authenticated with GitHub CLI."
    echo "Run: gh auth login"
    exit 1
fi

# Check if repo exists
if ! gh repo view $REPO &> /dev/null; then
    echo "Error: Repository $REPO does not exist."
    echo ""
    echo "Create the repository first:"
    echo "  gh repo create $REPO --public --description 'AI-Native Distributed Filesystem'"
    echo ""
    echo "Or push your local repo:"
    echo "  git remote add origin https://github.com/$REPO.git"
    echo "  git push -u origin main"
    exit 1
fi

echo "✓ Repository found"
echo ""

# Create milestones
echo "Creating milestones..."
gh api repos/$REPO/milestones -f title="v0.1.0 - Embedded Mode Foundation" \
  -f description="Core embedded filesystem with SQLite, local backend, and basic operations" \
  -f due_on="2025-03-31T00:00:00Z" 2>/dev/null || echo "  Milestone v0.1.0 may already exist"

gh api repos/$REPO/milestones -f title="v0.2.0 - Document Processing" \
  -f description="PDF, Excel, CSV parsers and semantic chunking" \
  -f due_on="2025-04-30T00:00:00Z" 2>/dev/null || echo "  Milestone v0.2.0 may already exist"

echo "✓ Milestones created"
echo ""

#############################################
# v0.1.0 ISSUES
#############################################

echo "Creating issues for v0.1.0 - Embedded Mode Foundation..."
echo ""

# Issue 1: Core Filesystem Operations
gh issue create --repo $REPO \
  --title "[v0.1.0] Implement core embedded filesystem operations" \
  --body "## Description
Implement basic read, write, and delete operations for the embedded filesystem based on the architecture document.

## Architecture Reference
See NEXUS_COMPREHENSIVE_ARCHITECTURE.md Section: \"Core Components\" and \"Embedded Mode Architecture\"

## Implementation Tasks

### File Operations
- [ ] Implement \`Embedded.read(path: str) -> bytes\` method
  - Map virtual path to physical location
  - Read from local backend
  - Handle file not found errors
  - Return file content as bytes

- [ ] Implement \`Embedded.write(path: str, content: bytes) -> None\` method
  - Validate virtual path
  - Create parent directories if needed
  - Write to local backend
  - Update metadata store
  - Invalidate cache

- [ ] Implement \`Embedded.delete(path: str) -> None\` method
  - Check if file exists
  - Remove from backend
  - Update metadata store
  - Remove from cache

### Error Handling
- [ ] Define custom exceptions (FileNotFoundError, PermissionError)
- [ ] Handle edge cases (empty paths, invalid characters)
- [ ] Add proper error messages

### Testing
- [ ] Unit tests for each operation
- [ ] Edge case tests
- [ ] Error handling tests
- [ ] Integration tests with metadata store

## Acceptance Criteria
- ✅ Can read files and get content as bytes
- ✅ Can write files with binary content
- ✅ Can delete files
- ✅ Proper error handling for all edge cases
- ✅ All tests pass with >80% coverage

## Related Files
- \`src/nexus/core/embedded.py\`
- \`src/nexus/core/exceptions.py\`
- \`tests/unit/test_embedded.py\`" \
  --label "enhancement,component: embedded,component: core,priority: high" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

echo "  ✓ Created: Core filesystem operations"

# Issue 2: SQLite Metadata Store
gh issue create --repo $REPO \
  --title "[v0.1.0] Implement SQLite metadata store with Alembic migrations" \
  --body "## Description
Create SQLite-based metadata store for tracking file paths, metadata, and relationships as defined in the architecture document.

## Architecture Reference
See NEXUS_COMPREHENSIVE_ARCHITECTURE.md Section: \"Complete Database Schema\"

## Database Schema

### Tables to Implement

\`\`\`sql
-- Core table for virtual path mapping
CREATE TABLE file_paths (
    path_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    virtual_path TEXT NOT NULL,
    backend_id UUID NOT NULL,
    physical_path TEXT NOT NULL,
    file_type VARCHAR(50),
    size_bytes BIGINT,
    content_hash VARCHAR(64),
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    deleted_at TIMESTAMP,
    UNIQUE(tenant_id, virtual_path)
);

-- File metadata
CREATE TABLE file_metadata (
    metadata_id UUID PRIMARY KEY,
    path_id UUID REFERENCES file_paths(path_id),
    key VARCHAR(255) NOT NULL,
    value JSONB,
    created_at TIMESTAMP NOT NULL
);

-- Content chunks for deduplication
CREATE TABLE content_chunks (
    chunk_id UUID PRIMARY KEY,
    content_hash VARCHAR(64) UNIQUE NOT NULL,
    size_bytes BIGINT NOT NULL,
    storage_path TEXT NOT NULL,
    ref_count INTEGER DEFAULT 1,
    created_at TIMESTAMP NOT NULL,
    last_accessed_at TIMESTAMP
);
\`\`\`

## Implementation Tasks

### SQLAlchemy Models
- [ ] Create \`FilePathModel\` in \`src/nexus/storage/models.py\`
- [ ] Create \`FileMetadataModel\`
- [ ] Create \`ContentChunkModel\`
- [ ] Add relationships between models
- [ ] Add indexes for performance

### Alembic Setup
- [ ] Initialize Alembic (\`alembic init alembic\`)
- [ ] Create initial migration
- [ ] Test migration up/down
- [ ] Add migration testing to CI

### Metadata Store Class
- [ ] Implement \`MetadataStore\` class
- [ ] Method: \`create_file_entry(path, metadata)\`
- [ ] Method: \`get_file_entry(path)\`
- [ ] Method: \`update_file_entry(path, metadata)\`
- [ ] Method: \`delete_file_entry(path)\`
- [ ] Method: \`list_files(path_prefix)\`
- [ ] Add connection pooling for embedded mode

### Testing
- [ ] Unit tests for each model
- [ ] Unit tests for MetadataStore methods
- [ ] Test migrations
- [ ] Test concurrent access

## Acceptance Criteria
- ✅ SQLite database created on initialization
- ✅ All tables and indexes created via migrations
- ✅ Can perform CRUD operations on file metadata
- ✅ Migrations can be applied and rolled back
- ✅ Tests pass with >80% coverage

## Related Files
- \`src/nexus/storage/models.py\`
- \`src/nexus/storage/metadata_store.py\`
- \`alembic/versions/*.py\`
- \`tests/unit/test_metadata_store.py\`" \
  --label "enhancement,component: embedded,component: storage,priority: high" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

echo "  ✓ Created: SQLite metadata store"

# Issue 3: Local Filesystem Backend
gh issue create --repo $REPO \
  --title "[v0.1.0] Implement local filesystem backend with content-addressable storage" \
  --body "## Description
Create local filesystem backend for storing actual file content with content-addressable storage (CAS) as described in the architecture.

## Architecture Reference
See NEXUS_COMPREHENSIVE_ARCHITECTURE.md Section: \"Backend Implementations\" - LocalFS Backend

## Content-Addressable Storage (CAS)

Files stored by content hash to enable deduplication:
\`\`\`
nexus-data/
└── files/
    ├── ab/
    │   └── cd/
    │       └── abcd1234...ef56  # SHA-256 hash
    └── 12/
        └── 34/
            └── 1234abcd...ef56
\`\`\`

## Implementation Tasks

### Backend Interface
- [ ] Create \`Backend\` abstract base class
  - \`read(path: str) -> bytes\`
  - \`write(path: str, content: bytes) -> str\`  # Returns content hash
  - \`delete(path: str) -> None\`
  - \`exists(path: str) -> bool\`

### LocalBackend Implementation
- [ ] Implement \`LocalBackend\` class
- [ ] Content-addressable storage with SHA-256 hashing
- [ ] Two-level directory structure (first 2 chars, next 2 chars)
- [ ] Reference counting for deduplication
- [ ] Atomic write operations (write to temp, then move)
- [ ] Directory creation with proper permissions

### File Operations
- [ ] \`write(content)\` - Store by content hash
  - Calculate SHA-256 hash
  - Check if content exists (dedup)
  - Write to CAS location if new
  - Increment ref count
  - Return content hash

- [ ] \`read(content_hash)\` - Read by hash
  - Validate hash format
  - Check if file exists
  - Read and return content
  - Update last_accessed timestamp

- [ ] \`delete(content_hash)\` - Delete by hash
  - Decrement ref count
  - Delete file if ref count = 0
  - Clean up empty directories

### File Locking
- [ ] Use \`fcntl.flock\` for POSIX systems
- [ ] Use \`msvcrt.locking\` for Windows
- [ ] Implement lock timeout and retry logic

### Testing
- [ ] Unit tests for each operation
- [ ] Test content deduplication
- [ ] Test atomic writes
- [ ] Test concurrent access
- [ ] Test file locking
- [ ] Test cleanup of orphaned files

## Acceptance Criteria
- ✅ Files stored in content-addressable format
- ✅ Duplicate content only stored once
- ✅ Atomic write operations
- ✅ Proper file locking
- ✅ Directory structure created automatically
- ✅ Tests pass with >80% coverage

## Related Files
- \`src/nexus/storage/backend.py\`
- \`src/nexus/storage/backends/local.py\`
- \`tests/unit/test_local_backend.py\`" \
  --label "enhancement,component: embedded,component: storage,priority: high" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

echo "  ✓ Created: Local filesystem backend"

# Issue 4: Virtual Path Router
gh issue create --repo $REPO \
  --title "[v0.1.0] Implement virtual path routing with namespace support" \
  --body "## Description
Create path router that maps virtual paths to backend storage locations, supporting multiple namespaces.

## Architecture Reference
See NEXUS_COMPREHENSIVE_ARCHITECTURE.md Section: \"Path Namespace Conventions\"

## Path Namespaces

\`\`\`
/
├── workspace/        # Agent scratch space (hot tier, ephemeral)
├── shared/           # Shared tenant data (warm tier, persistent)
├── external/         # Pass-through backends (no content storage)
├── system/           # System metadata (admin-only)
└── archives/         # Cold storage (read-only)
\`\`\`

## Implementation Tasks

### PathRouter Class
- [ ] Implement \`PathRouter\` class
- [ ] Register namespace handlers
- [ ] Route paths to appropriate backends
- [ ] Support tenant isolation

### Path Operations
- [ ] \`normalize_path(path)\` - Normalize to canonical form
  - Remove \`.\` and \`..\`
  - Remove trailing slashes
  - Convert backslashes to forward slashes

- [ ] \`validate_path(path)\` - Validate path format
  - Check for invalid characters
  - Check for security issues (path traversal)
  - Ensure path starts with \`/\`

- [ ] \`parse_path(path)\` - Extract namespace and tenant
  - Parse \`/workspace/{tenant}/{agent}/...\`
  - Return namespace, tenant_id, remaining path

- [ ] \`route_path(path)\` - Map to backend
  - Determine which backend handles this path
  - Return backend instance and physical path

### Namespace Configuration
- [ ] Define namespace rules in config
- [ ] Support read-only namespaces (e.g., \`/archives/\`)
- [ ] Support admin-only namespaces (e.g., \`/system/\`)

### Testing
- [ ] Test path normalization edge cases
- [ ] Test path validation (security)
- [ ] Test namespace routing
- [ ] Test tenant isolation

## Acceptance Criteria
- ✅ Virtual paths map correctly to physical locations
- ✅ Path validation prevents security issues
- ✅ Namespace routing works for all defined namespaces
- ✅ Tenant isolation enforced
- ✅ Tests pass with >80% coverage

## Related Files
- \`src/nexus/core/path_router.py\`
- \`tests/unit/test_path_router.py\`" \
  --label "enhancement,component: embedded,component: core,priority: high" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

echo "  ✓ Created: Virtual path router"

# Issue 5: File Discovery Operations
gh issue create --repo $REPO \
  --title "[v0.1.0] Implement file discovery operations (list, glob, grep)" \
  --body "## Description
Implement file discovery and search operations for embedded mode.

## Architecture Reference
See NEXUS_COMPREHENSIVE_ARCHITECTURE.md Section: \"Python SDK Interface\" - File Discovery

## Implementation Tasks

### List Operation
- [ ] Implement \`list(path, recursive=False)\` method
  - Query metadata store for files in path
  - Support recursive listing
  - Return list of file metadata (name, size, modified time)
  - Sort by name by default

### Glob Operation
- [ ] Implement \`glob(pattern, path='/')\` method
  - Support standard glob patterns: \`*\`, \`**\`, \`?\`, \`[...]\`
  - Use \`pathlib.Path.match()\` or custom implementation
  - Return list of matching file paths
  - Examples:
    - \`**/*.py\` - All Python files recursively
    - \`data/*.csv\` - All CSV files in data directory
    - \`test_*.py\` - All test files

### Grep Operation
- [ ] Implement \`grep(pattern, path='/', file_pattern=None)\` method
  - Search file contents using regex
  - Optionally filter files by glob pattern
  - Return matches with: file path, line number, matched line
  - Support case-insensitive search option
  - Handle binary files gracefully

### Performance Optimization
- [ ] Use metadata store for file discovery (don't scan filesystem)
- [ ] Implement result streaming for large result sets
- [ ] Add result limits to prevent memory issues

### Testing
- [ ] Test list with various directory structures
- [ ] Test glob with complex patterns
- [ ] Test grep with regex patterns
- [ ] Test performance with many files
- [ ] Test binary file handling

## Acceptance Criteria
- ✅ List operation works recursively and non-recursively
- ✅ Glob supports standard patterns
- ✅ Grep searches file contents accurately
- ✅ Performance is acceptable (can handle 10K+ files)
- ✅ Tests pass with >80% coverage

## Related Files
- \`src/nexus/core/embedded.py\`
- \`src/nexus/core/file_ops.py\`
- \`tests/unit/test_file_ops.py\`" \
  --label "enhancement,component: embedded,component: core,priority: medium" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

echo "  ✓ Created: File discovery operations"

# Issue 6: In-Memory Cache
gh issue create --repo $REPO \
  --title "[v0.1.0] Implement in-memory LRU cache for file content" \
  --body "## Description
Create in-memory LRU cache for frequently accessed files to improve read performance.

## Architecture Reference
See NEXUS_COMPREHENSIVE_ARCHITECTURE.md Section: \"Performance & Optimization\" - Caching

## Performance Targets
- Read latency: <10ms for cached files
- Cache hit rate: >50% for typical workloads
- Memory usage: Configurable, default 100MB

## Implementation Tasks

### CacheManager Class
- [ ] Implement \`CacheManager\` using LRU eviction policy
- [ ] Use \`cachetools.LRUCache\` or custom implementation
- [ ] Thread-safe operations (use locks)
- [ ] Configurable max size in MB

### Cache Operations
- [ ] \`get(key: str) -> Optional[bytes]\` - Get cached content
- [ ] \`put(key: str, content: bytes) -> None\` - Add to cache
- [ ] \`invalidate(key: str) -> None\` - Remove from cache
- [ ] \`clear() -> None\` - Clear entire cache

### Cache Key Strategy
- [ ] Use content hash as cache key
- [ ] Include file path in metadata for debugging

### Cache Invalidation
- [ ] Invalidate on write operations
- [ ] Invalidate on delete operations
- [ ] Support cache warming (preload commonly used files)

### Metrics
- [ ] Track cache hits and misses
- [ ] Track hit rate (hits / total requests)
- [ ] Track memory usage
- [ ] Expose metrics via \`get_stats()\` method

### Configuration
- [ ] Add \`cache_size_mb\` to \`EmbeddedConfig\`
- [ ] Add \`enable_cache\` flag
- [ ] Support disabling cache for testing

### Testing
- [ ] Test LRU eviction works correctly
- [ ] Test cache invalidation on writes
- [ ] Test thread safety with concurrent access
- [ ] Test memory limits enforced
- [ ] Test cache hit rate tracking

## Acceptance Criteria
- ✅ Cache improves read performance for repeated reads
- ✅ Memory usage stays within configured limits
- ✅ Cache invalidates correctly on updates
- ✅ Thread-safe for concurrent access
- ✅ Metrics accurately track hit rate
- ✅ Tests pass with >80% coverage

## Related Files
- \`src/nexus/core/cache.py\`
- \`tests/unit/test_cache.py\`" \
  --label "enhancement,component: embedded,performance,priority: medium" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

echo "  ✓ Created: In-memory cache"

# Issue 7: CLI Interface
gh issue create --repo $REPO \
  --title "[v0.1.0] Implement basic CLI interface with Click" \
  --body "## Description
Create command-line interface for basic file operations using Click and Rich for beautiful output.

## Architecture Reference
See NEXUS_COMPREHENSIVE_ARCHITECTURE.md Section: \"Python SDK Interface\"

## CLI Commands

### \`nexus init [PATH]\`
Initialize a new Nexus workspace

\`\`\`bash
nexus init ./my-workspace
# Creates:
# ./my-workspace/
# ├── nexus-data/
# │   ├── nexus.db
# │   └── files/
# ├── workspace/
# └── shared/
\`\`\`

### \`nexus ls [PATH] [--recursive]\`
List files in directory

\`\`\`bash
nexus ls /workspace
nexus ls /workspace --recursive
\`\`\`

### \`nexus cat <PATH>\`
Display file contents

\`\`\`bash
nexus cat /workspace/data.txt
\`\`\`

### \`nexus write <PATH> <CONTENT>\`
Write content to file

\`\`\`bash
nexus write /workspace/data.txt \"Hello World\"
echo \"Hello World\" | nexus write /workspace/data.txt -
\`\`\`

### \`nexus cp <SOURCE> <DEST>\`
Copy file

\`\`\`bash
nexus cp /workspace/source.txt /workspace/dest.txt
\`\`\`

### \`nexus rm <PATH>\`
Delete file

\`\`\`bash
nexus rm /workspace/data.txt
\`\`\`

### \`nexus glob <PATTERN>\`
Find files matching pattern

\`\`\`bash
nexus glob \"**/*.py\"
\`\`\`

### \`nexus grep <PATTERN> [PATH]\`
Search file contents

\`\`\`bash
nexus grep \"TODO\" /workspace
\`\`\`

## Implementation Tasks

### CLI Setup
- [ ] Use Click for CLI framework
- [ ] Use Rich for colored output and tables
- [ ] Add \`--help\` for all commands
- [ ] Add \`--version\` flag
- [ ] Add \`--data-dir\` global option

### Error Handling
- [ ] Beautiful error messages with Rich
- [ ] Exit codes: 0 (success), 1 (error)
- [ ] Handle FileNotFoundError gracefully
- [ ] Handle keyboard interrupt (Ctrl+C)

### Output Formatting
- [ ] Use Rich tables for \`ls\` output
- [ ] Use syntax highlighting for \`cat\` output
- [ ] Use progress bars for long operations
- [ ] Color-code file types (directory, file, etc.)

### Configuration
- [ ] Read from \`~/.nexus/config.yaml\`
- [ ] Support environment variables (\`NEXUS_DATA_DIR\`)
- [ ] Command-line args override config

### Testing
- [ ] Unit tests for each command
- [ ] Test with various file types
- [ ] Test error handling
- [ ] Test help messages

## Acceptance Criteria
- ✅ All commands work as specified
- ✅ Beautiful output with colors and formatting
- ✅ Clear error messages
- ✅ \`--help\` documentation is complete
- ✅ Tests pass with >80% coverage

## Related Files
- \`src/nexus/cli.py\`
- \`tests/unit/test_cli.py\`

## Good First Issue
This is a great issue for new contributors! Requires minimal knowledge of the core system." \
  --label "enhancement,component: cli,priority: medium,good first issue" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

echo "  ✓ Created: CLI interface"

# Issue 8: Comprehensive Testing
gh issue create --repo $REPO \
  --title "[v0.1.0] Add comprehensive test suite for embedded mode" \
  --body "## Description
Create comprehensive test suite with unit, integration, and performance tests for embedded mode.

## Testing Strategy
See NEXUS_COMPREHENSIVE_ARCHITECTURE.md Section: \"Testing Strategy\"

## Test Coverage Goals
- Overall coverage: >80%
- Core modules: >90%
- Critical paths: 100%

## Implementation Tasks

### Unit Tests
- [ ] Test \`Embedded\` class methods
- [ ] Test \`MetadataStore\` operations
- [ ] Test \`LocalBackend\` operations
- [ ] Test \`PathRouter\` logic
- [ ] Test \`CacheManager\` behavior
- [ ] Test CLI commands
- [ ] Test exception handling

### Integration Tests
- [ ] Test end-to-end file operations
- [ ] Test concurrent read/write operations
- [ ] Test cache integration with backend
- [ ] Test metadata store + backend integration
- [ ] Test CLI integration with embedded mode

### Performance Tests
- [ ] Benchmark read operations (target: <10ms for cached)
- [ ] Benchmark write operations (target: <50ms)
- [ ] Test with large files (100MB+)
- [ ] Test with many files (10,000+)
- [ ] Test concurrent operations

### Test Fixtures
- [ ] Create temporary test directories
- [ ] Create sample files (text, binary)
- [ ] Create mock backends for testing
- [ ] Clean up after tests

### Test Utilities
- [ ] Helper functions for creating test data
- [ ] Assertions for file content
- [ ] Performance measurement utilities

### CI Configuration
- [ ] Configure pytest in \`pyproject.toml\`
- [ ] Add coverage reporting
- [ ] Add test markers (unit, integration, slow)
- [ ] Fail build if coverage < 80%

## Test Organization
\`\`\`
tests/
├── unit/
│   ├── test_embedded.py
│   ├── test_metadata_store.py
│   ├── test_local_backend.py
│   ├── test_path_router.py
│   ├── test_cache.py
│   └── test_cli.py
├── integration/
│   ├── test_e2e_operations.py
│   ├── test_concurrent_access.py
│   └── test_cli_integration.py
├── performance/
│   ├── test_benchmarks.py
│   └── test_load.py
└── conftest.py  # Shared fixtures
\`\`\`

## Acceptance Criteria
- ✅ Test coverage >80% overall
- ✅ All unit tests pass
- ✅ All integration tests pass
- ✅ Performance tests meet targets
- ✅ CI configured and running
- ✅ Test documentation written

## Related Files
- \`tests/**/*.py\`
- \`pytest.ini\` or \`pyproject.toml\`
- \`.github/workflows/test.yml\`" \
  --label "testing,component: embedded,priority: high" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

echo "  ✓ Created: Comprehensive testing"

# Issue 9: Documentation
gh issue create --repo $REPO \
  --title "[v0.1.0] Write comprehensive documentation for embedded mode" \
  --body "## Description
Create comprehensive documentation for using and developing embedded mode.

## Documentation Needed

### Getting Started Guide (\`docs/getting-started.md\`)
- [ ] Installation instructions
- [ ] Quick start example
- [ ] Basic operations walkthrough
- [ ] Common use cases
- [ ] Troubleshooting

### API Reference (\`docs/api-reference.md\`)
- [ ] \`Embedded\` class documentation
- [ ] \`EmbeddedConfig\` options
- [ ] All public methods with examples
- [ ] Exception documentation
- [ ] Type hints reference

### Architecture Overview (\`docs/architecture.md\`)
- [ ] System architecture diagram
- [ ] Component descriptions
- [ ] Data flow diagrams
- [ ] Path routing explanation
- [ ] Content-addressable storage explanation

### Development Guide (\`docs/development.md\`)
- [ ] Setting up dev environment
- [ ] Running tests
- [ ] Code style guidelines
- [ ] Contributing workflow
- [ ] Adding new backends

### Examples (\`examples/\`)
- [ ] \`examples/basic_operations.py\`
- [ ] \`examples/file_discovery.py\`
- [ ] \`examples/concurrent_access.py\`
- [ ] \`examples/custom_config.py\`

### CLI Documentation (\`docs/cli.md\`)
- [ ] All commands with examples
- [ ] Configuration options
- [ ] Common workflows
- [ ] Troubleshooting

### Docstrings
- [ ] Add Google-style docstrings to all public APIs
- [ ] Include type hints
- [ ] Include examples in docstrings
- [ ] Keep docstrings concise but complete

## Documentation Tools
- [ ] Use Markdown for documentation
- [ ] Use MkDocs for site generation (optional)
- [ ] Use Sphinx for API docs (optional)

## Acceptance Criteria
- ✅ All documentation files created
- ✅ Examples work and are tested
- ✅ Docstrings complete for all public APIs
- ✅ Getting started guide allows new users to get started in <15 minutes
- ✅ Documentation reviewed by team

## Related Files
- \`docs/*.md\`
- \`examples/*.py\`
- \`README.md\`

## Good First Issue
This is a great issue for new contributors! Requires minimal knowledge of the implementation." \
  --label "documentation,component: embedded,priority: medium,good first issue" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

echo "  ✓ Created: Documentation"

# Issue 10: CI/CD Pipeline
gh issue create --repo $REPO \
  --title "[v0.1.0] Setup CI/CD pipeline with GitHub Actions" \
  --body "## Description
Configure GitHub Actions for automated testing, linting, and deployment.

## Workflows to Create

### Test Workflow (\`.github/workflows/test.yml\`)
Runs on: Every push and PR

\`\`\`yaml
- Checkout code
- Set up Python 3.11, 3.12
- Install dependencies with uv
- Run pytest with coverage
- Upload coverage to Codecov
- Fail if coverage < 80%
\`\`\`

### Lint Workflow (\`.github/workflows/lint.yml\`)
Runs on: Every push and PR

\`\`\`yaml
- Checkout code
- Set up Python 3.11
- Install dependencies
- Run ruff check
- Run ruff format --check
- Run mypy type checking
\`\`\`

### Release Workflow (\`.github/workflows/release.yml\`)
Runs on: Git tag push (v*)

\`\`\`yaml
- Checkout code
- Set up Python 3.11
- Build package with uv
- Publish to PyPI
- Create GitHub release with notes
\`\`\`

### Documentation Workflow (\`.github/workflows/docs.yml\`)
Runs on: Push to main

\`\`\`yaml
- Build documentation
- Deploy to GitHub Pages (optional)
\`\`\`

## Implementation Tasks

### Test Workflow
- [ ] Create \`.github/workflows/test.yml\`
- [ ] Test on Python 3.11 and 3.12
- [ ] Matrix testing on Ubuntu, macOS, Windows
- [ ] Upload coverage reports
- [ ] Add status badge to README

### Lint Workflow
- [ ] Create \`.github/workflows/lint.yml\`
- [ ] Run ruff linting
- [ ] Run ruff formatting check
- [ ] Run mypy type checking
- [ ] Fail on any errors

### Release Workflow
- [ ] Create \`.github/workflows/release.yml\`
- [ ] Trigger on version tags
- [ ] Build distributions
- [ ] Publish to PyPI (requires secrets)
- [ ] Create GitHub release

### Branch Protection
- [ ] Require status checks to pass
- [ ] Require review before merge
- [ ] Require branches to be up to date

### Badges
- [ ] Add CI status badge to README
- [ ] Add coverage badge
- [ ] Add PyPI version badge
- [ ] Add license badge

## Secrets to Configure
- \`CODECOV_TOKEN\` - For coverage uploads
- \`PYPI_API_TOKEN\` - For publishing releases

## Acceptance Criteria
- ✅ Test workflow runs on all PRs
- ✅ Lint workflow catches style issues
- ✅ Release workflow publishes to PyPI
- ✅ Coverage reports uploaded
- ✅ Status badges in README
- ✅ Branch protection configured

## Related Files
- \`.github/workflows/test.yml\`
- \`.github/workflows/lint.yml\`
- \`.github/workflows/release.yml\`" \
  --label "enhancement,priority: high" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

echo "  ✓ Created: CI/CD pipeline"

echo ""
echo "✅ Created 10 issues for v0.1.0"
echo ""

#############################################
# v0.2.0 ISSUES
#############################################

echo "Creating issues for v0.2.0 - Document Processing..."
echo ""

# Issue 11: Parser System Architecture
gh issue create --repo $REPO \
  --title "[v0.2.0] Design and implement parser system architecture" \
  --body "## Description
Create extensible parser system for processing various document formats.

## Architecture Reference
See NEXUS_COMPREHENSIVE_ARCHITECTURE.md Section: \"Parser System Architecture\" and \"Rich Format Parsing\"

## Parser System Design

### Parser Interface
\`\`\`python
class Parser(ABC):
    @abstractmethod
    def can_parse(self, file_path: str, mime_type: str) -> bool:
        \"\"\"Check if this parser can handle the file\"\"\"

    @abstractmethod
    async def parse(self, content: bytes, metadata: dict) -> ParseResult:
        \"\"\"Parse file content and return structured data\"\"\"

    @property
    @abstractmethod
    def supported_formats(self) -> List[str]:
        \"\"\"List of supported file extensions\"\"\"
\`\`\`

### ParseResult Schema
\`\`\`python
class ParseResult:
    text: str                    # Extracted text
    metadata: dict              # File metadata
    structure: dict             # Document structure
    chunks: List[TextChunk]     # Semantic chunks
    images: List[ImageData]     # Extracted images
\`\`\`

## Implementation Tasks

### Core Parser System
- [ ] Create \`Parser\` abstract base class
- [ ] Create \`ParseResult\` data class
- [ ] Implement \`ParserRegistry\` for managing parsers
- [ ] Add parser selection logic (by extension and MIME type)

### Parser Registry
- [ ] Auto-discover parsers on initialization
- [ ] Register parsers by format
- [ ] Handle multiple parsers for same format (priority)
- [ ] Provide \`get_parser(file_path)\` method

### Document Type Detection
- [ ] Use \`python-magic\` for MIME type detection
- [ ] Fallback to extension-based detection
- [ ] Handle compressed files (.gz, .zip)
- [ ] Handle text encoding detection

### Error Handling
- [ ] Define \`ParserError\` exception
- [ ] Graceful degradation (extract what's possible)
- [ ] Logging for failed parsing
- [ ] Return partial results when possible

### Testing
- [ ] Test parser registration
- [ ] Test parser selection logic
- [ ] Test error handling
- [ ] Test with various file types

## Acceptance Criteria
- ✅ Extensible parser system implemented
- ✅ Parser registry works
- ✅ Document type detection accurate
- ✅ Error handling robust
- ✅ Tests pass with >80% coverage

## Related Files
- \`src/nexus/parsers/base.py\`
- \`src/nexus/parsers/registry.py\`
- \`src/nexus/parsers/types.py\`
- \`tests/unit/test_parser_system.py\`" \
  --label "enhancement,component: parsers,priority: high" \
  --milestone "v0.2.0 - Document Processing"

echo "  ✓ Created: Parser system architecture"

# Issue 12: PDF Parser
gh issue create --repo $REPO \
  --title "[v0.2.0] Implement PDF parser with text and table extraction" \
  --body "## Description
Implement PDF parser that extracts text, images, tables, and metadata from PDF files.

## Architecture Reference
See NEXUS_COMPREHENSIVE_ARCHITECTURE.md Section: \"Rich Format Parsing\" - PDF Parser

## Features to Support

### Text Extraction
- Extract all text from PDF
- Preserve formatting where possible
- Handle multi-column layouts
- Support scanned PDFs (OCR) - optional for v0.2.0

### Table Extraction
- Detect tables in PDF
- Extract as structured data (rows/columns)
- Support merged cells
- Export as CSV or JSON

### Image Extraction
- Extract embedded images
- Save with original format
- Extract image metadata

### Metadata Extraction
- Title, author, subject
- Creation/modification dates
- Page count
- PDF version

## Implementation Tasks

### PDF Parser Class
- [ ] Create \`PDFParser\` class implementing \`Parser\` interface
- [ ] Use \`pypdf\` for basic text extraction
- [ ] Use \`pdfplumber\` for table extraction
- [ ] Support multi-page PDFs

### Text Extraction
- [ ] Extract text page by page
- [ ] Handle different encodings
- [ ] Preserve paragraph structure
- [ ] Remove headers/footers (optional)

### Table Extraction
- [ ] Detect tables using \`pdfplumber\`
- [ ] Extract table data
- [ ] Return as list of dicts
- [ ] Handle malformed tables

### Image Extraction
- [ ] Extract images using \`pypdf\`
- [ ] Save as separate files or embed in result
- [ ] Support JPEG, PNG formats
- [ ] Store image metadata

### Metadata Extraction
- [ ] Extract PDF metadata
- [ ] Include in ParseResult
- [ ] Handle missing metadata gracefully

### Testing
- [ ] Test with various PDF types
- [ ] Test with scanned PDFs
- [ ] Test with tables
- [ ] Test with images
- [ ] Test with encrypted PDFs (should fail gracefully)

## Sample Test Files
Create \`tests/fixtures/\` with:
- Simple text PDF
- PDF with tables
- PDF with images
- Multi-page PDF
- Encrypted PDF

## Acceptance Criteria
- ✅ Can extract text from PDF files
- ✅ Can extract tables as structured data
- ✅ Can extract images
- ✅ Metadata extracted correctly
- ✅ Handles errors gracefully
- ✅ Tests pass with >80% coverage

## Related Files
- \`src/nexus/parsers/pdf.py\`
- \`tests/unit/test_pdf_parser.py\`
- \`tests/fixtures/*.pdf\`" \
  --label "enhancement,component: parsers,priority: high" \
  --milestone "v0.2.0 - Document Processing"

echo "  ✓ Created: PDF parser"

# Issue 13: Excel and CSV Parsers
gh issue create --repo $REPO \
  --title "[v0.2.0] Implement Excel and CSV parsers with schema detection" \
  --body "## Description
Implement parsers for Excel (.xlsx, .xls) and CSV files with automatic schema detection.

## Architecture Reference
See NEXUS_COMPREHENSIVE_ARCHITECTURE.md Section: \"Rich Format Parsing\" - Excel/CSV Parser

## Features to Support

### Excel Parser
- Read .xlsx files (Excel 2007+)
- Read .xls files (Excel 97-2003)
- Support multiple sheets
- Preserve cell formatting (colors, bold, etc.)
- Extract formulas
- Extract charts (optional)

### CSV Parser
- Auto-detect delimiter (comma, tab, semicolon)
- Auto-detect encoding (UTF-8, Latin-1, etc.)
- Handle quoted fields
- Support different line endings

### Schema Detection
- Detect column types (string, number, date, boolean)
- Detect headers automatically
- Handle missing values
- Infer data types

## Implementation Tasks

### Excel Parser
- [ ] Create \`ExcelParser\` class
- [ ] Use \`openpyxl\` for .xlsx files
- [ ] Use \`xlrd\` for .xls files (legacy)
- [ ] Support multiple sheets
- [ ] Extract to JSON format

### CSV Parser
- [ ] Create \`CSVParser\` class
- [ ] Use \`pandas.read_csv\` with auto-detection
- [ ] Detect delimiter automatically
- [ ] Handle different encodings
- [ ] Extract to JSON format

### Schema Detection
- [ ] Implement \`detect_schema(df)\` function
- [ ] Detect column types using pandas
- [ ] Detect headers (first row vs data)
- [ ] Handle numeric columns with strings
- [ ] Handle date columns

### Data Transformation
- [ ] Convert to standard JSON format
- [ ] Preserve data types
- [ ] Handle NaN/null values
- [ ] Handle large files efficiently

### ParseResult Format
\`\`\`json
{
  \"sheets\": [
    {
      \"name\": \"Sheet1\",
      \"rows\": 100,
      \"columns\": 10,
      \"schema\": {
        \"Name\": \"string\",
        \"Age\": \"integer\",
        \"Date\": \"date\"
      },
      \"data\": [
        {\"Name\": \"Alice\", \"Age\": 30, \"Date\": \"2024-01-01\"},
        ...
      ]
    }
  ]
}
\`\`\`

### Testing
- [ ] Test with various Excel files
- [ ] Test with CSV files (different delimiters)
- [ ] Test with large files (100K+ rows)
- [ ] Test schema detection accuracy
- [ ] Test encoding detection

## Sample Test Files
Create \`tests/fixtures/\` with:
- Simple Excel file (.xlsx)
- Legacy Excel file (.xls)
- CSV with comma delimiter
- CSV with tab delimiter
- CSV with encoding issues
- Large CSV file (100K rows)

## Acceptance Criteria
- ✅ Can parse Excel files (.xlsx, .xls)
- ✅ Can parse CSV files with various delimiters
- ✅ Schema detection works accurately
- ✅ Handles large files efficiently
- ✅ Tests pass with >80% coverage

## Related Files
- \`src/nexus/parsers/excel.py\`
- \`src/nexus/parsers/csv.py\`
- \`src/nexus/parsers/schema.py\`
- \`tests/unit/test_excel_parser.py\`
- \`tests/unit/test_csv_parser.py\`" \
  --label "enhancement,component: parsers,priority: high" \
  --milestone "v0.2.0 - Document Processing"

echo "  ✓ Created: Excel and CSV parsers"

# Issue 14: Image and Document Parsers
gh issue create --repo $REPO \
  --title "[v0.2.0] Implement parsers for images and common document formats" \
  --body "## Description
Implement parsers for images (JPEG, PNG) and common document formats (DOCX, TXT, JSON).

## Features to Support

### Image Parser
- Extract image metadata (dimensions, format, EXIF)
- Generate image thumbnail (optional)
- Extract text from images using OCR (optional for v0.2.0)
- Support: JPEG, PNG, GIF, WebP

### DOCX Parser
- Extract text from Word documents
- Preserve document structure (headings, paragraphs)
- Extract tables
- Extract images

### Text Parser
- Handle plain text files
- Auto-detect encoding
- Preserve formatting
- Handle large files

### JSON Parser
- Parse JSON files
- Validate JSON structure
- Extract metadata
- Handle nested structures

## Implementation Tasks

### Image Parser
- [ ] Create \`ImageParser\` class
- [ ] Use \`Pillow\` for image operations
- [ ] Extract EXIF metadata
- [ ] Get image dimensions
- [ ] Detect image format

### DOCX Parser
- [ ] Create \`DocxParser\` class
- [ ] Use \`python-docx\` library
- [ ] Extract text by paragraph
- [ ] Extract tables
- [ ] Preserve document structure

### Text Parser
- [ ] Create \`TextParser\` class
- [ ] Auto-detect encoding with \`chardet\`
- [ ] Handle UTF-8, Latin-1, etc.
- [ ] Split into paragraphs/lines

### JSON Parser
- [ ] Create \`JSONParser\` class
- [ ] Validate JSON syntax
- [ ] Pretty print JSON
- [ ] Extract metadata (keys, depth)

### Testing
- [ ] Test with various image formats
- [ ] Test with DOCX files
- [ ] Test with different text encodings
- [ ] Test with complex JSON structures
- [ ] Test with malformed files

## Sample Test Files
Create \`tests/fixtures/\` with:
- JPEG image with EXIF data
- PNG image
- DOCX with tables and images
- Text file with UTF-8 encoding
- Text file with Latin-1 encoding
- JSON file with nested structure

## Acceptance Criteria
- ✅ Can parse image files and extract metadata
- ✅ Can parse DOCX files and extract text/tables
- ✅ Can parse text files with various encodings
- ✅ Can parse and validate JSON files
- ✅ Tests pass with >80% coverage

## Related Files
- \`src/nexus/parsers/image.py\`
- \`src/nexus/parsers/docx.py\`
- \`src/nexus/parsers/text.py\`
- \`src/nexus/parsers/json.py\`
- \`tests/unit/test_parsers.py\`

## Good First Issue
The text and JSON parsers are good for new contributors!" \
  --label "enhancement,component: parsers,priority: medium,good first issue" \
  --milestone "v0.2.0 - Document Processing"

echo "  ✓ Created: Image and document parsers"

# Issue 15: Semantic Chunking
gh issue create --repo $REPO \
  --title "[v0.2.0] Implement semantic text chunking for document processing" \
  --body "## Description
Implement semantic text chunking to split documents into meaningful chunks for vector embeddings.

## Architecture Reference
See NEXUS_COMPREHENSIVE_ARCHITECTURE.md Section: \"Content Processing Pipeline\" - Semantic Chunking

## Chunking Strategies

### Fixed-Size Chunking
- Split by character count
- Overlap between chunks
- Simple but effective

### Sentence-Based Chunking
- Split by sentences
- Combine sentences to reach target size
- Better semantic coherence

### Paragraph-Based Chunking
- Split by paragraphs
- Good for structured documents

### Semantic Chunking (Advanced)
- Use embeddings to find natural boundaries
- Group similar sentences together
- Most sophisticated approach

## Implementation Tasks

### Chunking Interface
\`\`\`python
class Chunker(ABC):
    @abstractmethod
    def chunk(self, text: str, max_size: int) -> List[TextChunk]:
        \"\"\"Split text into chunks\"\"\"

class TextChunk:
    text: str
    start_pos: int
    end_pos: int
    metadata: dict
\`\`\`

### Fixed-Size Chunker
- [ ] Implement \`FixedSizeChunker\`
- [ ] Support character-based and token-based limits
- [ ] Add overlap parameter (default: 50 characters)
- [ ] Respect word boundaries

### Sentence Chunker
- [ ] Implement \`SentenceChunker\`
- [ ] Use \`nltk\` or regex for sentence detection
- [ ] Combine sentences to reach target size
- [ ] Handle edge cases (abbreviations, etc.)

### Paragraph Chunker
- [ ] Implement \`ParagraphChunker\`
- [ ] Split by double newlines
- [ ] Handle different paragraph markers
- [ ] Preserve paragraph structure

### Chunk Metadata
- [ ] Add source document info
- [ ] Add position in original document
- [ ] Add chunk size and token count
- [ ] Add chunk hash for deduplication

### Testing
- [ ] Test with various document types
- [ ] Test with different chunk sizes
- [ ] Test chunk overlap
- [ ] Verify semantic coherence
- [ ] Performance testing with large documents

## Chunking Parameters
\`\`\`python
ChunkingConfig(
    strategy=\"sentence\",  # fixed, sentence, paragraph
    max_size=512,         # tokens or characters
    overlap=50,           # overlap between chunks
    respect_boundaries=True  # don't split mid-sentence
)
\`\`\`

## Acceptance Criteria
- ✅ Multiple chunking strategies implemented
- ✅ Chunks have proper metadata
- ✅ Configurable chunk size and overlap
- ✅ Good performance with large documents
- ✅ Tests pass with >80% coverage

## Related Files
- \`src/nexus/parsers/chunking.py\`
- \`tests/unit/test_chunking.py\`" \
  --label "enhancement,component: parsers,priority: medium" \
  --milestone "v0.2.0 - Document Processing"

echo "  ✓ Created: Semantic chunking"

# Issue 16: Qdrant Integration
gh issue create --repo $REPO \
  --title "[v0.2.0] Integrate Qdrant for vector storage and semantic search" \
  --body "## Description
Integrate Qdrant vector database for storing document embeddings and enabling semantic search.

## Architecture Reference
See NEXUS_COMPREHENSIVE_ARCHITECTURE.md Section: \"Semantic Search\" and \"Vector DB\"

## Implementation Tasks

### Qdrant Setup
- [ ] Add \`qdrant-client\` dependency
- [ ] Support embedded mode (local storage)
- [ ] Support server mode (remote Qdrant)
- [ ] Configure in \`EmbeddedConfig\`

### Vector Store Interface
\`\`\`python
class VectorStore(ABC):
    @abstractmethod
    async def create_collection(self, name: str, vector_size: int):
        \"\"\"Create a new collection\"\"\"

    @abstractmethod
    async def upsert(self, collection: str, vectors: List[Vector]):
        \"\"\"Insert or update vectors\"\"\"

    @abstractmethod
    async def search(self, collection: str, query: Vector, limit: int):
        \"\"\"Search for similar vectors\"\"\"
\`\`\`

### Qdrant Implementation
- [ ] Create \`QdrantVectorStore\` class
- [ ] Implement collection management
- [ ] Implement vector upsert
- [ ] Implement similarity search
- [ ] Support metadata filtering

### Collection Schema
\`\`\`python
{
    \"collection\": \"nexus_documents\",
    \"vector_size\": 1536,  # OpenAI text-embedding-3-large
    \"distance\": \"Cosine\",
    \"payload_schema\": {
        \"document_id\": \"keyword\",
        \"chunk_id\": \"keyword\",
        \"text\": \"text\",
        \"metadata\": \"json\"
    }
}
\`\`\`

### Indexing Pipeline
- [ ] Parse document → chunks
- [ ] Generate embeddings for chunks (placeholder for v0.2.0)
- [ ] Store vectors in Qdrant
- [ ] Store metadata with vectors

### Search Implementation
- [ ] Convert query to embedding
- [ ] Search Qdrant for similar vectors
- [ ] Return results with scores
- [ ] Support metadata filtering

### Configuration
- [ ] Add Qdrant config to \`EmbeddedConfig\`
- [ ] Support embedded and server modes
- [ ] Configure collection name and vector size

### Testing
- [ ] Test collection creation
- [ ] Test vector insertion
- [ ] Test similarity search
- [ ] Test metadata filtering
- [ ] Test with embedded Qdrant

## Acceptance Criteria
- ✅ Qdrant embedded mode works
- ✅ Can create collections
- ✅ Can insert vectors with metadata
- ✅ Can search for similar vectors
- ✅ Metadata filtering works
- ✅ Tests pass with >80% coverage

## Related Files
- \`src/nexus/storage/vector_store.py\`
- \`src/nexus/storage/qdrant.py\`
- \`tests/unit/test_vector_store.py\`

## Notes
- Embedding generation will be implemented in v0.3.0
- For v0.2.0, focus on Qdrant integration and interface
- Use mock embeddings for testing" \
  --label "enhancement,component: storage,component: ai,priority: high" \
  --milestone "v0.2.0 - Document Processing"

echo "  ✓ Created: Qdrant integration"

# Issue 17: Document Processing Pipeline
gh issue create --repo $REPO \
  --title "[v0.2.0] Implement end-to-end document processing pipeline" \
  --body "## Description
Create end-to-end pipeline for processing documents: parse → chunk → index.

## Architecture Reference
See NEXUS_COMPREHENSIVE_ARCHITECTURE.md Section: \"Content Processing Pipeline (Supermemory)\"

## Pipeline Stages

1. **Document Detection** - Identify document type
2. **Parsing** - Extract text and metadata
3. **Chunking** - Split into semantic chunks
4. **Embedding** - Generate vectors (v0.3.0)
5. **Indexing** - Store in vector database

## Implementation Tasks

### Pipeline Interface
\`\`\`python
class DocumentPipeline:
    async def process_file(self, path: str) -> ProcessingResult:
        \"\"\"Process a file through the pipeline\"\"\"

    async def process_batch(self, paths: List[str]) -> List[ProcessingResult]:
        \"\"\"Process multiple files\"\"\"

class ProcessingResult:
    success: bool
    document_id: str
    chunks: int
    errors: List[str]
\`\`\`

### Pipeline Implementation
- [ ] Create \`DocumentPipeline\` class
- [ ] Integrate parser registry
- [ ] Integrate chunking
- [ ] Integrate vector storage (placeholder embeddings)
- [ ] Support batch processing

### Processing Workflow
\`\`\`python
async def process_file(path: str):
    # 1. Read file
    content = await file_store.read(path)

    # 2. Detect document type
    mime_type = detect_mime_type(content)

    # 3. Select parser
    parser = parser_registry.get_parser(path, mime_type)

    # 4. Parse document
    result = await parser.parse(content)

    # 5. Chunk text
    chunks = chunker.chunk(result.text)

    # 6. Generate embeddings (placeholder for v0.2.0)
    vectors = await generate_embeddings(chunks)

    # 7. Store in vector DB
    await vector_store.upsert(collection, vectors)
\`\`\`

### Error Handling
- [ ] Handle parsing failures gracefully
- [ ] Continue processing other files on error
- [ ] Log errors with context
- [ ] Return partial results when possible

### Progress Tracking
- [ ] Add progress callback for batch processing
- [ ] Emit events for each stage
- [ ] Track processing time per stage

### CLI Integration
- [ ] Add \`nexus index <path>\` command
- [ ] Support recursive indexing
- [ ] Show progress bar with Rich
- [ ] Display summary statistics

### Testing
- [ ] Test pipeline with various file types
- [ ] Test batch processing
- [ ] Test error handling
- [ ] Test progress tracking
- [ ] Integration test with full pipeline

## Acceptance Criteria
- ✅ Pipeline processes documents end-to-end
- ✅ Batch processing works
- ✅ Error handling robust
- ✅ Progress tracking implemented
- ✅ CLI command works
- ✅ Tests pass with >80% coverage

## Related Files
- \`src/nexus/core/pipeline.py\`
- \`src/nexus/cli.py\`
- \`tests/integration/test_pipeline.py\`" \
  --label "enhancement,component: parsers,priority: high" \
  --milestone "v0.2.0 - Document Processing"

echo "  ✓ Created: Document processing pipeline"

# Issue 18: v0.2.0 Documentation
gh issue create --repo $REPO \
  --title "[v0.2.0] Document parsing system and pipeline usage" \
  --body "## Description
Create comprehensive documentation for the document processing system.

## Documentation Needed

### Parser Guide (\`docs/parsers.md\`)
- [ ] Overview of parser system
- [ ] Supported file formats
- [ ] How to use each parser
- [ ] Parser configuration options
- [ ] Custom parser development guide

### Semantic Search Guide (\`docs/semantic-search.md\`)
- [ ] Overview of semantic search
- [ ] How chunking works
- [ ] Vector storage explanation
- [ ] Search API usage
- [ ] Best practices

### Examples
- [ ] \`examples/parse_pdf.py\`
- [ ] \`examples/parse_excel.py\`
- [ ] \`examples/process_documents.py\`
- [ ] \`examples/semantic_chunking.py\`

### API Documentation
- [ ] Document all parser classes
- [ ] Document chunking API
- [ ] Document vector store API
- [ ] Document pipeline API

### CLI Documentation
- [ ] Document \`nexus index\` command
- [ ] Document search commands
- [ ] Examples and use cases

## Acceptance Criteria
- ✅ All documentation complete
- ✅ Examples work and are tested
- ✅ API documentation complete
- ✅ User can learn system in <30 minutes

## Related Files
- \`docs/parsers.md\`
- \`docs/semantic-search.md\`
- \`examples/*.py\`

## Good First Issue
Great for new contributors!" \
  --label "documentation,component: parsers,priority: medium,good first issue" \
  --milestone "v0.2.0 - Document Processing"

echo "  ✓ Created: v0.2.0 documentation"

echo ""
echo "✅ Created 8 issues for v0.2.0"
echo ""
echo "======================================"
echo "Summary:"
echo "  • v0.1.0: 10 issues created"
echo "  • v0.2.0: 8 issues created"
echo "  • Total: 18 issues"
echo "======================================"
echo ""
echo "View issues:"
echo "  gh issue list --milestone 'v0.1.0 - Embedded Mode Foundation'"
echo "  gh issue list --milestone 'v0.2.0 - Document Processing'"
echo ""
echo "Next steps:"
echo "  1. Review issues on GitHub"
echo "  2. Assign issues to team members"
echo "  3. Set up GitHub Projects board"
echo "  4. Start with high-priority v0.1.0 issues"
