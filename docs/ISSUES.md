# GitHub Issues for Nexus

This document lists all planned issues for Nexus development, organized by milestone.

## Setup Instructions

### 1. Install GitHub CLI

```bash
# macOS
brew install gh

# Linux
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update
sudo apt install gh

# Windows
winget install --id GitHub.cli
```

### 2. Authenticate with GitHub

```bash
gh auth login
```

### 3. Create Issues

```bash
# Update the repository name in the script first
# Edit: scripts/create-issues.sh
# Change: REPO="yourusername/nexus"

# Run the script
./scripts/create-issues.sh
```

## Milestone: v0.1.0 - Embedded Mode Foundation

### Core Filesystem (#1)
**Title:** Implement core embedded filesystem operations
**Labels:** `enhancement`, `component: embedded`, `component: core`, `priority: high`
**Description:** Implement basic read, write, and delete operations

**Tasks:**
- [ ] Implement `Embedded.read(path)` method
- [ ] Implement `Embedded.write(path, content)` method
- [ ] Implement `Embedded.delete(path)` method
- [ ] Handle errors and edge cases
- [ ] Add unit tests
- [ ] Add integration tests

---

### SQLite Metadata Store (#2)
**Title:** Implement SQLite metadata store
**Labels:** `enhancement`, `component: embedded`, `component: storage`, `priority: high`
**Description:** Create SQLite-based metadata store for file tracking

**Tasks:**
- [ ] Design database schema
- [ ] Implement SQLAlchemy models
- [ ] Create migration scripts (Alembic)
- [ ] Implement CRUD operations
- [ ] Add indexes for performance
- [ ] Add tests

---

### Local Filesystem Backend (#3)
**Title:** Implement local filesystem backend
**Labels:** `enhancement`, `component: embedded`, `component: storage`, `priority: high`
**Description:** Create local filesystem backend for content storage

**Tasks:**
- [ ] Implement `LocalBackend` class
- [ ] Implement read/write/delete operations
- [ ] Handle directory creation
- [ ] Implement content-addressable storage (CAS)
- [ ] Add file locking
- [ ] Add tests

---

### Virtual Path Routing (#4)
**Title:** Implement virtual path routing
**Labels:** `enhancement`, `component: embedded`, `component: core`, `priority: high`
**Description:** Map virtual paths to backend storage locations

**Tasks:**
- [ ] Implement `PathRouter` class
- [ ] Support path namespaces (/workspace, /shared, etc.)
- [ ] Implement path validation
- [ ] Handle path normalization
- [ ] Add tests

---

### File Operations (#5)
**Title:** Implement basic file operations (list, glob, grep)
**Labels:** `enhancement`, `component: embedded`, `component: core`, `priority: medium`
**Description:** File discovery and search operations

**Tasks:**
- [ ] Implement `list(path, recursive)` method
- [ ] Implement `glob(pattern)` method
- [ ] Implement `grep(pattern, path)` method
- [ ] Support recursive operations
- [ ] Add tests

---

### Caching Layer (#6)
**Title:** Implement in-memory caching layer
**Labels:** `enhancement`, `component: embedded`, `performance`, `priority: medium`
**Description:** LRU cache for frequently accessed files

**Tasks:**
- [ ] Implement `CacheManager` class
- [ ] Use LRU eviction policy
- [ ] Configurable cache size
- [ ] Cache invalidation on write
- [ ] Add metrics (hit rate, etc.)
- [ ] Add tests

---

### CLI Interface (#7)
**Title:** Implement basic CLI interface
**Labels:** `enhancement`, `component: cli`, `priority: medium`, `good first issue`
**Description:** Command-line interface for file operations

**Commands:**
- `nexus init` - Initialize workspace
- `nexus ls <path>` - List files
- `nexus cat <path>` - Read file
- `nexus write <path> <content>` - Write file
- `nexus rm <path>` - Delete file

---

### Unit Tests (#8)
**Title:** Add comprehensive unit tests for embedded mode
**Labels:** `testing`, `component: embedded`, `priority: high`
**Description:** Complete test coverage for embedded mode

**Tasks:**
- [ ] Test file operations
- [ ] Test path routing
- [ ] Test metadata store
- [ ] Test local backend
- [ ] Test cache
- [ ] Test CLI commands
- [ ] Aim for >80% coverage

---

### Documentation (#9)
**Title:** Write developer documentation for embedded mode
**Labels:** `documentation`, `component: embedded`, `priority: medium`, `good first issue`
**Description:** Comprehensive documentation for developers

**Docs Needed:**
- [ ] Getting started guide
- [ ] API reference
- [ ] Architecture overview
- [ ] Development setup
- [ ] Testing guide
- [ ] Examples

---

### CI/CD Pipeline (#10)
**Title:** Setup CI/CD pipeline with GitHub Actions
**Labels:** `enhancement`, `priority: high`
**Description:** Automated testing and deployment

**Workflows:**
- [ ] Test workflow (pytest)
- [ ] Lint workflow (ruff, mypy)
- [ ] Coverage reporting (codecov)
- [ ] Build workflow
- [ ] Release workflow

---

## Labels

### Type Labels
- `bug` - Something isn't working
- `enhancement` - New feature or request
- `documentation` - Documentation improvements
- `question` - Further information requested
- `good first issue` - Good for newcomers
- `help wanted` - Extra attention needed

### Priority Labels
- `priority: critical` - Critical priority
- `priority: high` - High priority
- `priority: medium` - Medium priority
- `priority: low` - Low priority

### Component Labels
- `component: embedded` - Embedded mode
- `component: server` - Server mode
- `component: distributed` - Distributed mode
- `component: core` - Core filesystem
- `component: storage` - Storage backends
- `component: api` - REST API
- `component: cli` - Command-line interface
- `component: ai` - AI/ML features
- `component: agents` - Agent memory
- `component: parsers` - Document parsing
- `component: mcp` - MCP integration
- `component: auth` - Authentication
- `component: jobs` - Job system

### Status Labels
- `status: blocked` - Blocked
- `status: in progress` - In progress
- `status: needs review` - Needs review
- `status: needs testing` - Needs testing

### Special Labels
- `breaking change` - Breaking changes
- `dependencies` - Dependency updates
- `performance` - Performance improvements
- `security` - Security issues
- `testing` - Testing-related
- `refactor` - Code refactoring

## Viewing Issues

```bash
# List all issues
gh issue list

# List issues for v0.1.0
gh issue list --milestone "v0.1.0 - Embedded Mode Foundation"

# Filter by label
gh issue list --label "component: embedded"

# View specific issue
gh issue view 1
```

## Managing Issues

```bash
# Assign issue
gh issue edit 1 --add-assignee @me

# Add labels
gh issue edit 1 --add-label "priority: high"

# Close issue
gh issue close 1

# Reopen issue
gh issue reopen 1
```

## Creating More Issues

To add issues for future milestones (v0.2.0, v0.3.0, etc.), follow the same pattern in `scripts/create-issues.sh` or create them manually:

```bash
gh issue create \
  --title "Your issue title" \
  --body "Issue description" \
  --label "enhancement,component: embedded" \
  --milestone "v0.2.0"
```
