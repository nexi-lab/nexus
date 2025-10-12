#!/bin/bash
set -e

# Script to create GitHub issues for Nexus
# Requires: gh (GitHub CLI) to be installed and authenticated
# Usage: ./scripts/create-issues.sh

REPO="nexi-lab/nexus"  # Update with your GitHub username

echo "Creating GitHub issues for Nexus..."
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

echo "Creating labels..."
gh label create "component: embedded" --color "1d76db" --description "Embedded mode functionality" --force || true
gh label create "component: server" --color "1d76db" --description "Server mode functionality" --force || true
gh label create "component: core" --color "5319e7" --description "Core filesystem functionality" --force || true
gh label create "component: storage" --color "5319e7" --description "Storage backend implementations" --force || true
gh label create "component: ai" --color "ff6347" --description "AI/ML features" --force || true
gh label create "component: agents" --color "ff6347" --description "Agent memory and workspace features" --force || true
gh label create "component: parsers" --color "ff6347" --description "Document parsing functionality" --force || true
gh label create "component: cli" --color "5319e7" --description "Command-line interface" --force || true
gh label create "priority: high" --color "d93f0b" --description "High priority" --force || true
gh label create "priority: medium" --color "fbca04" --description "Medium priority" --force || true
gh label create "priority: low" --color "0e8a16" --description "Low priority" --force || true
gh label create "good first issue" --color "7057ff" --description "Good for newcomers" --force || true

echo "Labels created!"
echo ""

echo "Creating milestone: v0.1.0"
gh api repos/$REPO/milestones -f title="v0.1.0 - Embedded Mode Foundation" -f description="Core embedded filesystem functionality" -f due_on="2025-12-31T00:00:00Z" 2>/dev/null || echo "Milestone may already exist"

echo ""
echo "Creating issues for v0.1.0 - Embedded Mode Foundation..."
echo ""

# v0.1.0 Issues

gh issue create \
  --title "Implement core embedded filesystem operations" \
  --body "## Description
Implement basic read, write, and delete operations for the embedded filesystem.

## Tasks
- [ ] Implement \`Embedded.read(path)\` method
- [ ] Implement \`Embedded.write(path, content)\` method
- [ ] Implement \`Embedded.delete(path)\` method
- [ ] Handle errors and edge cases
- [ ] Add unit tests
- [ ] Add integration tests

## Acceptance Criteria
- Can read, write, and delete files
- Proper error handling for missing files
- All tests pass" \
  --label "enhancement,component: embedded,component: core,priority: high" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

gh issue create \
  --title "Implement SQLite metadata store" \
  --body "## Description
Create SQLite-based metadata store for tracking file paths, metadata, and relationships.

## Tasks
- [ ] Design database schema
- [ ] Implement SQLAlchemy models
- [ ] Create migration scripts (Alembic)
- [ ] Implement CRUD operations
- [ ] Add indexes for performance
- [ ] Add tests

## Schema Tables
- \`file_paths\` - Virtual path mapping
- \`file_metadata\` - File attributes
- \`file_chunks\` - Content chunks

## Acceptance Criteria
- SQLite database created on init
- Can store and query file metadata
- Migrations work correctly" \
  --label "enhancement,component: embedded,component: storage,priority: high" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

gh issue create \
  --title "Implement local filesystem backend" \
  --body "## Description
Create local filesystem backend for storing actual file content.

## Tasks
- [ ] Implement \`LocalBackend\` class
- [ ] Implement read/write/delete operations
- [ ] Handle directory creation
- [ ] Implement content-addressable storage (CAS)
- [ ] Add file locking
- [ ] Add tests

## Acceptance Criteria
- Files stored in \`nexus-data/files/\`
- Content-addressable storage works
- File operations are atomic
- Proper error handling" \
  --label "enhancement,component: embedded,component: storage,priority: high" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

gh issue create \
  --title "Implement virtual path routing" \
  --body "## Description
Create path router that maps virtual paths to backend storage locations.

## Tasks
- [ ] Implement \`PathRouter\` class
- [ ] Support path namespaces (/workspace, /shared, etc.)
- [ ] Implement path validation
- [ ] Handle path normalization
- [ ] Add tests

## Path Namespaces
- \`/workspace/\` - Agent scratch space
- \`/shared/\` - Shared data
- \`/system/\` - System metadata

## Acceptance Criteria
- Virtual paths map to physical locations
- Path validation works
- Proper error handling for invalid paths" \
  --label "enhancement,component: embedded,component: core,priority: high" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

gh issue create \
  --title "Implement basic file operations (list, glob, grep)" \
  --body "## Description
Implement file discovery and search operations.

## Tasks
- [ ] Implement \`list(path, recursive)\` method
- [ ] Implement \`glob(pattern)\` method for pattern matching
- [ ] Implement \`grep(pattern, path)\` method for content search
- [ ] Support recursive operations
- [ ] Add tests

## Acceptance Criteria
- Can list directory contents
- Glob patterns work (e.g., \`**/*.py\`)
- Grep can search file contents
- Performance is acceptable" \
  --label "enhancement,component: embedded,component: core,priority: medium" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

gh issue create \
  --title "Implement in-memory caching layer" \
  --body "## Description
Create in-memory LRU cache for frequently accessed files.

## Tasks
- [ ] Implement \`CacheManager\` class
- [ ] Use LRU eviction policy
- [ ] Configurable cache size
- [ ] Cache invalidation on write
- [ ] Add metrics (hit rate, etc.)
- [ ] Add tests

## Acceptance Criteria
- Cache speeds up repeated reads
- Memory usage stays within limits
- Cache invalidates correctly on updates" \
  --label "enhancement,component: embedded,performance,priority: medium" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

gh issue create \
  --title "Implement basic CLI interface" \
  --body "## Description
Create command-line interface for basic file operations.

## Commands
- \`nexus init\` - Initialize workspace
- \`nexus ls <path>\` - List files
- \`nexus cat <path>\` - Read file
- \`nexus write <path> <content>\` - Write file
- \`nexus rm <path>\` - Delete file

## Tasks
- [ ] Implement CLI commands using Click
- [ ] Add \`--help\` documentation
- [ ] Add colored output with Rich
- [ ] Handle errors gracefully
- [ ] Add tests

## Acceptance Criteria
- All commands work
- Good user experience
- Clear error messages" \
  --label "enhancement,component: cli,priority: medium,good first issue" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

gh issue create \
  --title "Add comprehensive unit tests for embedded mode" \
  --body "## Description
Create comprehensive test suite for embedded mode functionality.

## Tasks
- [ ] Test file operations (read/write/delete)
- [ ] Test path routing
- [ ] Test metadata store
- [ ] Test local backend
- [ ] Test cache
- [ ] Test CLI commands
- [ ] Add fixtures and helpers
- [ ] Aim for >80% code coverage

## Acceptance Criteria
- Test coverage >80%
- All edge cases covered
- Tests run fast (<5 seconds)
- CI/CD integration ready" \
  --label "testing,component: embedded,priority: high" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

gh issue create \
  --title "Write developer documentation for embedded mode" \
  --body "## Description
Create comprehensive documentation for using and developing embedded mode.

## Documentation Needed
- [ ] Getting started guide
- [ ] API reference
- [ ] Architecture overview
- [ ] Development setup
- [ ] Testing guide
- [ ] Examples

## Files
- \`docs/embedded-mode.md\`
- \`docs/api-reference.md\`
- \`docs/development.md\`
- \`examples/embedded_*.py\`

## Acceptance Criteria
- New developers can get started quickly
- All public APIs documented
- Examples work" \
  --label "documentation,component: embedded,priority: medium,good first issue" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

gh issue create \
  --title "Setup CI/CD pipeline with GitHub Actions" \
  --body "## Description
Configure GitHub Actions for automated testing and deployment.

## Workflows
- [ ] Test workflow (pytest)
- [ ] Lint workflow (ruff, mypy)
- [ ] Coverage reporting (codecov)
- [ ] Build workflow (build package)
- [ ] Release workflow (publish to PyPI)

## Files
- \`.github/workflows/test.yml\`
- \`.github/workflows/lint.yml\`
- \`.github/workflows/release.yml\`

## Acceptance Criteria
- Tests run on every PR
- Coverage reported
- Can publish releases automatically" \
  --label "enhancement,priority: high" \
  --milestone "v0.1.0 - Embedded Mode Foundation"

echo ""
echo "✅ Issues created successfully!"
echo ""
echo "Next steps:"
echo "1. View issues: gh issue list --milestone 'v0.1.0 - Embedded Mode Foundation'"
echo "2. Update repo name in this script: $REPO"
echo "3. Assign issues to team members"
echo "4. Set up GitHub Projects board"
