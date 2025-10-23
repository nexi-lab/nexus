# Nexus Public Release Cleanup Plan

**Version:** 0.4.0
**Status:** Pre-Release
**Last Updated:** 2025-10-23

This document outlines the comprehensive cleanup plan for Nexus's first public release. The plan is organized into 8 major areas with specific action items and priorities.

---

## Table of Contents

- [1. Root Directory Cleanup](#1-root-directory-cleanup)
- [2. Documentation Cleanup](#2-documentation-cleanup)
- [3. Code Cleanup](#3-code-cleanup)
- [4. Configuration & Build Cleanup](#4-configuration--build-cleanup)
- [5. Security & Compliance](#5-security--compliance)
- [6. Testing & Quality Assurance](#6-testing--quality-assurance)
- [7. Release Preparation](#7-release-preparation)
- [8. Polish & User Experience](#8-polish--user-experience)
- [Implementation Priority](#implementation-priority)
- [Estimated Effort](#estimated-effort)
- [Quick Start Cleanup Script](#quick-start-cleanup-script)

---

## 1. Root Directory Cleanup

**Priority:** HIGH

### Files to Remove

- ❌ `None` - Empty file that shouldn't exist
- ❌ `metadata.db`, `nexus.db`, `nexus-metadata.db` - Development database files
- ❌ `coverage.xml`, `.coverage` - Test coverage artifacts (keep in .gitignore)
- ❌ `NEXUS_COMPREHENSIVE_ARCHITECTURE.md` (468KB) - Already in .gitignore but present in repo

### Directories to Remove

- ❌ `demo-data-no-parse/` - Temporary demo data
- ❌ `test-fix-data/` - Test data directory
- ❌ `nexus-examples-data/` - Example data artifacts
- ❌ `nexus-examples-workspace/` - Temporary workspace
- ❌ `nexus-sdk-examples-data/` - SDK example artifacts
- ❌ `my-data/` - Personal test data
- ❌ `cas/`, `dirs/` - Local storage directories
- ❌ `htmlcov/` - Coverage report HTML (keep in .gitignore)
- ❌ `.benchmarks/` - Benchmark artifacts

### Files to Review/Consolidate

- 📝 `NAMESPACE_VISIBILITY_CHANGES.md` - Move to docs/development/ or remove if outdated
- ✅ `DEPLOYMENT_QUICK_START.md` - Moved to `docs/deployment/QUICK_START.md`
- ✅ Deployment scripts: `deploy-gcp.sh`, `deploy-gcp-docker.sh`, `deploy-gcp.example.sh` - Moved to `scripts/`
- ✅ `check_server.py` - Moved to `scripts/`
- ✅ `test_gcs_backend.py` - Moved to `tests/integration/`

---

## 2. Documentation Cleanup

**Priority:** HIGH

### Consolidate & Update

#### 1. Architecture Documentation

- ✅ Keep: `docs/architecture/ARCHITECTURE.md` (80 lines, concise)
- ❌ Remove: `NEXUS_COMPREHENSIVE_ARCHITECTURE.md` (15,894 lines - too verbose for repo)
- 📝 Consider: Add condensed version to docs/ if needed

#### 2. Main README.md (91KB)

- 🔍 Review for first-time user clarity
- ✂️ Extract advanced topics to separate docs
- ✨ Add clear "Quick Start in 5 Minutes" section
- 📸 Consider adding architecture diagrams
- 🔗 Fix broken documentation links

#### 3. Documentation Index (`docs/README.md`)

- ❌ Remove outdated version references ("v0.1.0 as current")
- ✅ Update to reflect v0.3.9/v0.4.0 features
- 🔗 Fix broken internal links
- 📋 Add missing documentation pages

#### 4. Deployment Documentation

- ✅ Consolidate: `DEPLOYMENT_QUICK_START.md` → `docs/deployment/QUICK_START.md`
- ✅ Move deployment scripts to `scripts/`
- ✅ Update references in `docs/deployment/` with new script paths

#### 5. Getting Started Guides

- ✅ Keep: `docs/getting-started/` directory
- 📝 Verify all guides are up-to-date with v0.3.9
- ✨ Add troubleshooting section

### Create Missing Documentation

- 📝 **SECURITY.md** - Security policy and vulnerability reporting
- 📝 **CODE_OF_CONDUCT.md** - Community guidelines
- 📝 **FAQ.md** - Common questions and answers
- 📝 **ROADMAP.md** - Public development roadmap (optional)

### Documentation Quality Checks

- 🔍 Search for TODOs in all .md files
- 🔗 Validate all internal and external links
- ✂️ Remove references to unreleased features
- ✅ Ensure code examples are tested and working

---

## 3. Code Cleanup

**Priority:** MEDIUM

### Source Code Review

#### 1. Remove TODOs/FIXMEs

- 🔍 Found in: `tests/unit/core/test_remote_fs.py`, `examples/py_demo/embedded_demo.py`, `examples/e2b/` files
- 📝 Either fix them or create GitHub issues

#### 2. Plugin Directories

- 📁 `nexus-plugin-anthropic/` - Consider moving to separate repo or clearly document as monorepo structure
- 📁 `nexus-plugin-skill-seekers/` - Same as above
- 📝 Update main README to explain plugin architecture

#### 3. Examples Cleanup

- ✅ Keep well-documented examples (31 Python files)
- 🔍 Test all examples to ensure they work
- 📝 Add README to each example subdirectory
- ✂️ Remove or update deprecated examples

#### 4. LLM Module (feature/llm-provider-abstraction branch)

- ✅ Files look clean: `src/nexus/llm/` (staged in current branch)
- 📝 Ensure docs are updated: `docs/llm_provider.md`, `src/nexus/llm/README.md`

### Code Quality

- 🧪 Run full test suite and ensure 100% pass
- 🎨 Run `ruff format` and `ruff check` on entire codebase
- 📊 Run `mypy src/nexus` and fix type errors
- 🔒 Security scan for hardcoded secrets/API keys

---

## 4. Configuration & Build Cleanup

**Priority:** MEDIUM

### Configuration Files

- 📝 `.env.docker.example` - Ensure complete and documented
- ❌ Remove `.env` from repo (keep in .gitignore only)
- ✅ `pyproject.toml` - Looks good, verify version is correct
- 📝 Update `alembic.ini` if needed

### Build & Deploy Scripts

**Moved to `scripts/`:**
- ✅ `deploy-gcp.sh`
- ✅ `deploy-gcp-docker.sh`
- ✅ `deploy-gcp.example.sh`
- ✅ `check_server.py`
- 📝 `start-server.sh` (keep in root for convenience)

**Keep in `scripts/`:**
- ✅ `setup.sh`
- ✅ `run_benchmarks.sh`
- ✅ Other utility scripts

**Action Items:**
- 📝 Add README in `scripts/` explaining each script

### Git Cleanup

- 🌿 Delete merged feature branches locally
- 📝 `.gitignore` - Already well configured, but verify:
  - ✅ NEXUS_COMPREHENSIVE_ARCHITECTURE.md is ignored (line 150)
  - ✅ `None` file is ignored (line 152)
  - ✅ Database files are ignored

---

## 5. Security & Compliance

**Priority:** HIGH

### Pre-Release Security Audit

- 🔒 Scan for hardcoded API keys, tokens, passwords
  ```bash
  grep -r "api_key\|token\|password\|secret" --include="*.py" src/
  ```
- 🔒 Review `.env.docker.example` for sensitive defaults
- 🔒 Check git history for accidentally committed secrets
- 📝 Add SECURITY.md with vulnerability reporting process

### License & Headers

- ✅ LICENSE file present (Apache 2.0)
- 🔍 Verify all source files have license headers
- 📝 Update copyright year to 2024/2025 if needed

### Dependencies

- 🔍 Review `pyproject.toml` dependencies for security advisories
- 📝 Update dependencies to latest stable versions
- 🔒 Add `requirements-security.txt` with pinned versions for production

---

## 6. Testing & Quality Assurance

**Priority:** HIGH

### Test Coverage

- 🧪 Run full test suite: `pytest tests/`
- 📊 Check coverage: Currently at 83% for time-travel feature
- 🎯 Target: >80% overall coverage
- 📝 Document how to run tests in CONTRIBUTING.md

### Integration Testing

- 🧪 Test all CLI commands
- 🧪 Test all examples in `examples/` directory
- 🧪 Test deployment scripts
- 🧪 Test with both SQLite and PostgreSQL

### CI/CD

- ✅ GitHub Actions workflows present (test.yml, lint.yml)
- 🔍 Verify all workflows pass
- 📝 Add badge status to README (already present)

---

## 7. Release Preparation

**Priority:** HIGH

### Version Management

- 📝 Update version in `pyproject.toml` (currently 0.3.9, plan shows 0.4.0)
- 📝 Update CHANGELOG.md with v0.4.0 features
- 📝 Review README.md version reference (line 9)

### Package Metadata

- ✅ `pyproject.toml` looks good:
  - Description, keywords, classifiers ✓
  - URLs (homepage, docs, issues) ✓
  - License ✓
- 📝 Verify author email is correct (currently: team@nexus.example.com)

### Release Checklist

1. ✅ All tests pass
2. ✅ Documentation is complete and accurate
3. ✅ CHANGELOG.md is updated
4. ✅ Version bumped in pyproject.toml
5. ✅ Security audit complete
6. ✅ Examples tested
7. ✅ Create GitHub release with notes
8. ✅ Tag release: `git tag v0.4.0`
9. ✅ Build package: `python -m build`
10. ✅ Upload to PyPI

---

## 8. Polish & User Experience

**Priority:** MEDIUM

### README Improvements

- ✨ Add "Why Nexus?" section highlighting key benefits
- 📸 Add architecture diagram image
- 🎬 Consider adding demo GIF/video
- 📝 Simplify Quick Start (make it copy-paste ready)
- 🏆 Add comparison table with similar projects
- 📱 Add badges: build status, coverage, PyPI version, downloads

### Documentation Website

- 📝 `mkdocs.yml` exists - consider deploying to GitHub Pages or Read the Docs
- 🌐 Build and test: `mkdocs serve`
- 📝 Add navigation improvements in mkdocs.yml

### Community Setup

- 📝 Add SUPPORT.md (how to get help)
- 💬 Consider setting up GitHub Discussions
- 📧 Set up proper contact email (replace team@nexus.example.com)

---

## Implementation Priority

### Phase 1: Critical (Do First) 🔴

1. Remove sensitive data / security audit
2. Clean up root directory (files & folders)
3. Fix broken documentation links
4. Ensure all tests pass
5. Update version numbers consistently

### Phase 2: Important (Do Before Release) 🟡

1. Consolidate documentation
2. Update README for clarity
3. Fix code TODOs or create issues
4. Add SECURITY.md, CODE_OF_CONDUCT.md
5. Test all examples

### Phase 3: Nice to Have (Polish) 🟢

1. Add architecture diagrams
2. Set up documentation website
3. Create demo video
4. Separate plugin repos
5. Add comparison table

---

## Estimated Effort

- **Phase 1 (Critical)**: 4-6 hours
- **Phase 2 (Important)**: 8-12 hours
- **Phase 3 (Polish)**: 6-10 hours
- **Total**: 18-28 hours for complete cleanup

---

## Quick Start Cleanup Script

Here's a bash script to automate Phase 1 cleanup:

```bash
#!/bin/bash
# cleanup-for-release.sh

echo "🧹 Nexus Pre-Release Cleanup"

# Remove temporary files
rm -f None metadata.db nexus.db nexus-metadata.db coverage.xml .coverage
rm -rf htmlcov/ demo-data-no-parse/ test-fix-data/ nexus-examples-data/
rm -rf nexus-examples-workspace/ nexus-sdk-examples-data/ my-data/ cas/ dirs/

# Move files to proper locations (COMPLETED in v0.4.0)
# ✅ mv DEPLOYMENT_QUICK_START.md docs/deployment/QUICK_START.md
# ✅ mv deploy-gcp*.sh scripts/
# ✅ mv check_server.py scripts/
# ✅ mv test_gcs_backend.py tests/integration/

# Update git
git add -A
git status

echo "✅ Cleanup complete! Review changes with 'git status'"
```

### Running the Script

```bash
chmod +x cleanup-for-release.sh
./cleanup-for-release.sh
```

---

## Next Steps

After completing this cleanup plan:

1. **Review Changes**: Carefully review all changes before committing
2. **Create PR**: Submit cleanup changes via pull request
3. **Update Docs**: Ensure all documentation reflects the cleaned state
4. **Test Everything**: Run full test suite and manual testing
5. **Security Review**: Final security audit before release
6. **Release**: Follow the release checklist in section 7

---

## Notes

- This plan was generated based on analysis of the codebase as of 2025-10-23
- Some items may require team discussion before implementation
- Adjust priorities based on release timeline
- Keep backup of important files before deletion

---

**Document Status:** Draft
**Requires Approval:** Yes
**Last Reviewed:** 2025-10-23
