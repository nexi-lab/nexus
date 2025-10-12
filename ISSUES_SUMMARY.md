# GitHub Issues Setup Summary

I've created a complete GitHub issues setup for Nexus v0.1.0. Here's what was created:

## Files Created

### 1. Issue Templates
- `.github/ISSUE_TEMPLATE/feature_request.md` - Template for feature requests
- `.github/ISSUE_TEMPLATE/bug_report.md` - Template for bug reports
- `.github/ISSUE_TEMPLATE/config.yml` - Issue template configuration

### 2. Labels Configuration
- `.github/labels.yml` - Complete label definitions with colors
- `.github/workflows/label-sync.yml` - Automatic label syncing workflow

### 3. Issue Creation Script
- `scripts/create-issues.sh` - Automated script to create all v0.1.0 issues
- `docs/ISSUES.md` - Complete documentation of all planned issues

## Quick Start

### Prerequisites
```bash
# Install GitHub CLI
brew install gh  # macOS
# or see docs/ISSUES.md for other platforms

# Authenticate
gh auth login
```

### Create Issues
```bash
# 1. Edit the script to set your repo name
nano scripts/create-issues.sh
# Change: REPO="yourusername/nexus"

# 2. Run the script
./scripts/create-issues.sh
```

## Issues for v0.1.0

The script will create 10 issues:

1. **Core embedded filesystem operations** (High Priority)
   - Read/write/delete operations
   - Labels: `component: embedded`, `component: core`, `priority: high`

2. **SQLite metadata store** (High Priority)
   - Database schema and migrations
   - Labels: `component: embedded`, `component: storage`, `priority: high`

3. **Local filesystem backend** (High Priority)
   - Content-addressable storage
   - Labels: `component: embedded`, `component: storage`, `priority: high`

4. **Virtual path routing** (High Priority)
   - Path namespace mapping
   - Labels: `component: embedded`, `component: core`, `priority: high`

5. **File operations (list, glob, grep)** (Medium Priority)
   - File discovery and search
   - Labels: `component: embedded`, `component: core`, `priority: medium`

6. **In-memory caching layer** (Medium Priority)
   - LRU cache implementation
   - Labels: `component: embedded`, `performance`, `priority: medium`

7. **Basic CLI interface** (Medium Priority)
   - Command-line tools
   - Labels: `component: cli`, `priority: medium`, `good first issue`

8. **Comprehensive unit tests** (High Priority)
   - 80%+ code coverage
   - Labels: `testing`, `component: embedded`, `priority: high`

9. **Developer documentation** (Medium Priority)
   - Getting started guides
   - Labels: `documentation`, `component: embedded`, `good first issue`

10. **CI/CD pipeline** (High Priority)
    - GitHub Actions workflows
    - Labels: `enhancement`, `priority: high`

## Labels Created

### Type Labels
- bug, enhancement, documentation, question
- good first issue, help wanted

### Priority Labels
- priority: critical, high, medium, low

### Component Labels
- component: embedded, server, distributed, core, storage, api, cli
- component: ai, agents, parsers, mcp, auth, jobs

### Status Labels
- status: blocked, in progress, needs review, needs testing

### Special Labels
- breaking change, dependencies, performance, security, testing, refactor

## Next Steps

1. **Install GitHub CLI** if not already installed
2. **Update the repository name** in `scripts/create-issues.sh`
3. **Run the script** to create all issues
4. **Set up GitHub Projects** board for milestone tracking
5. **Assign issues** to team members
6. **Start development** on high-priority issues

## Viewing Issues

```bash
# List all issues
gh issue list

# List v0.1.0 issues
gh issue list --milestone "v0.1.0 - Embedded Mode Foundation"

# Filter by label
gh issue list --label "component: embedded"
gh issue list --label "priority: high"
gh issue list --label "good first issue"
```

## Manual Issue Creation

If you prefer to create issues manually or through the GitHub web interface, see `docs/ISSUES.md` for complete issue descriptions and task lists.

## Automating Label Sync

The label sync workflow (`.github/workflows/label-sync.yml`) will automatically sync labels from `.github/labels.yml` to your repository whenever you push changes to that file.
