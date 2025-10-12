# GitHub Repository Setup for Nexus

This guide will help you create the GitHub repository and set up issues for v0.1.0 and v0.2.0.

## Quick Setup

If the repository doesn't exist yet, run:

\`\`\`bash
./scripts/setup-github-repo.sh
\`\`\`

This will:
1. Create the GitHub repository (if it doesn't exist)
2. Push your local code to GitHub
3. Create labels
4. Create 18 issues for v0.1.0 and v0.2.0

## Manual Setup

### Step 1: Create Repository (if needed)

If you haven't created the repository on GitHub yet:

\`\`\`bash
gh repo create windoliver/nexus \
  --public \
  --description "AI-Native Distributed Filesystem"
\`\`\`

### Step 2: Push Code to GitHub

\`\`\`bash
git remote add origin https://github.com/windoliver/nexus.git
git push -u origin main
\`\`\`

### Step 3: Create Issues

Run the issue creation script:

\`\`\`bash
./scripts/create-v0.1-v0.2-issues.sh
\`\`\`

This will create:
- **v0.1.0 - Embedded Mode Foundation**: 10 issues
- **v0.2.0 - Document Processing**: 8 issues

## Issues Overview

### v0.1.0 - Embedded Mode Foundation (10 issues)

1. **Core embedded filesystem operations** - `priority: high`, `component: embedded`, `component: core`
   - Read/write/delete operations
   - Error handling
   - Integration with metadata store

2. **SQLite metadata store with Alembic migrations** - `priority: high`, `component: storage`
   - Database schema
   - SQLAlchemy models
   - Migration system

3. **Local filesystem backend with content-addressable storage** - `priority: high`, `component: storage`
   - CAS implementation
   - File deduplication
   - Reference counting

4. **Virtual path routing with namespace support** - `priority: high`, `component: core`
   - Path validation
   - Namespace routing
   - Tenant isolation

5. **File discovery operations (list, glob, grep)** - `priority: medium`, `component: core`
   - List files
   - Glob pattern matching
   - Content search

6. **In-memory LRU cache** - `priority: medium`, `performance`
   - Cache implementation
   - Cache invalidation
   - Metrics

7. **Basic CLI interface** - `priority: medium`, `component: cli`, `good first issue`
   - CLI commands
   - Beautiful output with Rich
   - Error handling

8. **Comprehensive test suite** - `priority: high`, `testing`
   - Unit tests
   - Integration tests
   - >80% coverage

9. **Comprehensive documentation** - `priority: medium`, `documentation`, `good first issue`
   - Getting started guide
   - API reference
   - Examples

10. **CI/CD pipeline** - `priority: high`
    - GitHub Actions workflows
    - Automated testing
    - Coverage reporting

### v0.2.0 - Document Processing (8 issues)

1. **Parser system architecture** - `priority: high`, `component: parsers`
   - Parser interface
   - Parser registry
   - Document type detection

2. **PDF parser** - `priority: high`, `component: parsers`
   - Text extraction
   - Table extraction
   - Image extraction

3. **Excel and CSV parsers** - `priority: high`, `component: parsers`
   - Excel (.xlsx, .xls) support
   - CSV parsing with auto-detection
   - Schema detection

4. **Image and document parsers** - `priority: medium`, `component: parsers`, `good first issue`
   - Image metadata extraction
   - DOCX parser
   - Text and JSON parsers

5. **Semantic text chunking** - `priority: medium`, `component: parsers`
   - Multiple chunking strategies
   - Chunk metadata
   - Configurable parameters

6. **Qdrant integration** - `priority: high`, `component: storage`, `component: ai`
   - Vector store interface
   - Qdrant embedded mode
   - Similarity search

7. **Document processing pipeline** - `priority: high`, `component: parsers`
   - End-to-end pipeline
   - Batch processing
   - CLI integration

8. **Documentation** - `priority: medium`, `documentation`, `good first issue`
   - Parser guide
   - Semantic search guide
   - Examples

## Viewing Issues

\`\`\`bash
# List all issues
gh issue list

# List v0.1.0 issues
gh issue list --milestone "v0.1.0 - Embedded Mode Foundation"

# List v0.2.0 issues
gh issue list --milestone "v0.2.0 - Document Processing"

# Filter by label
gh issue list --label "priority: high"
gh issue list --label "component: embedded"
gh issue list --label "good first issue"

# View specific issue
gh issue view 1
\`\`\`

## Managing Issues

\`\`\`bash
# Assign issue to yourself
gh issue edit 1 --add-assignee @me

# Add labels
gh issue edit 1 --add-label "in progress"

# Close issue
gh issue close 1 --comment "Completed"
\`\`\`

## GitHub Projects

Consider setting up a GitHub Projects board to track progress:

\`\`\`bash
gh project create "Nexus Development" --org windoliver
\`\`\`

Then add issues to the project board.

## Next Steps

1. **Review all issues** on GitHub
2. **Prioritize** which issues to work on first
3. **Assign issues** to team members
4. **Set up GitHub Projects** board (optional)
5. **Start with v0.1.0 high-priority issues**:
   - #1: Core filesystem operations
   - #2: SQLite metadata store
   - #3: Local filesystem backend
   - #4: Virtual path routing

## Labels Reference

All issues are tagged with appropriate labels:

- **Priority**: `priority: high`, `priority: medium`, `priority: low`
- **Component**: `component: embedded`, `component: storage`, `component: core`, `component: parsers`, `component: cli`, `component: ai`
- **Type**: `enhancement`, `testing`, `documentation`, `performance`
- **Special**: `good first issue` (for newcomers)

## Need Help?

- **GitHub CLI docs**: https://cli.github.com/manual/
- **GitHub Issues docs**: https://docs.github.com/issues
- **Nexus architecture**: See `NEXUS_COMPREHENSIVE_ARCHITECTURE.md`
