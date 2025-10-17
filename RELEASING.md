# Release Guide

This document describes how to create a new release of Nexus.

## Prerequisites

1. **PyPI Account Setup**
   - Create account at https://pypi.org
   - Enable 2FA (required)
   - Generate API token at https://pypi.org/manage/account/token/
   - Add token to GitHub Secrets as `PYPI_API_TOKEN`

2. **GitHub Permissions**
   - Write access to the repository
   - Ability to create tags and releases

3. **Local Setup**
   - All tests passing (`pytest`)
   - Clean working directory (`git status`)
   - Up-to-date with main branch

## Release Process

### 1. Update Version and Changelog

```bash
# Update version in pyproject.toml
# Current: version = "0.1.0"
# New:     version = "0.2.0"
vim pyproject.toml

# Update CHANGELOG.md
# Move items from [Unreleased] to new version section
# Add release date
vim CHANGELOG.md
```

Example CHANGELOG update:
```markdown
## [Unreleased]

## [0.2.0] - 2025-11-15

### Added
- UNIX-style file permissions
- chmod, chown, chgrp operations
...
```

### 2. Commit and Push Changes

```bash
# Commit version bump
git add pyproject.toml CHANGELOG.md
git commit -m "Bump version to 0.2.0"
git push origin main
```

### 3. Create and Push Tag

```bash
# Create annotated tag
git tag -a v0.2.0 -m "Release v0.2.0"

# Push tag (this triggers the release workflow)
git push origin v0.2.0
```

### 4. Automated Release

The GitHub Actions workflow (`.github/workflows/release.yml`) will automatically:

1. **Build Package**
   - Builds source distribution and wheel
   - Verifies package integrity with `twine check`

2. **Publish to PyPI**
   - Uploads to https://pypi.org/project/nexus-ai-fs/
   - Uses PyPI trusted publishing (no token needed if configured)

3. **Create GitHub Release**
   - Creates release at https://github.com/nexi-lab/nexus/releases
   - Extracts changelog for this version
   - Attaches distribution files

4. **Post-Release Checks**
   - Waits for PyPI to index the package
   - Tests installation from PyPI
   - Verifies CLI works

### 5. Verify Release

```bash
# Check PyPI
open https://pypi.org/project/nexus-ai-fs/

# Check GitHub Release
open https://github.com/nexi-lab/nexus/releases

# Test installation
pip install --upgrade nexus-ai-fs
nexus --version
```

## Manual Release (Fallback)

If automated release fails, you can release manually:

```bash
# Build package
pip install build twine
python -m build

# Check package
twine check dist/*

# Upload to PyPI (requires API token)
twine upload dist/*

# Create GitHub release manually
gh release create v0.2.0 \
  --title "Release v0.2.0" \
  --notes-file <(sed -n '/## \[0.2.0\]/,/## \[/p' CHANGELOG.md | sed '$d') \
  dist/*
```

## Release Checklist

Use this checklist for each release:

- [ ] All tests passing (`pytest`)
- [ ] Version bumped in `pyproject.toml`
- [ ] CHANGELOG.md updated with changes and release date
- [ ] Documentation updated (if needed)
- [ ] README.md examples still work
- [ ] Changes committed and pushed to main
- [ ] Git tag created and pushed
- [ ] GitHub Actions workflow completed successfully
- [ ] PyPI package published
- [ ] GitHub release created
- [ ] Installation tested: `pip install nexus-ai-fs==X.Y.Z`
- [ ] CLI works: `nexus --version`
- [ ] Announced release (Discord/Twitter/etc.)

## Version Numbering

Nexus follows [Semantic Versioning](https://semver.org/):

- **Major (X.0.0)**: Breaking changes, incompatible API changes
- **Minor (0.X.0)**: New features, backward-compatible
- **Patch (0.0.X)**: Bug fixes, backward-compatible

### Pre-release Versions

For pre-releases, use these suffixes:
- `0.2.0-alpha.1` - Early testing, unstable
- `0.2.0-beta.1` - Feature complete, testing
- `0.2.0-rc.1` - Release candidate, final testing

## Troubleshooting

### Workflow Fails

1. Check GitHub Actions logs
2. Verify PYPI_API_TOKEN is set correctly
3. Ensure version doesn't already exist on PyPI
4. Check that tests pass

### PyPI Upload Fails

```bash
# Common issues:
# 1. Version already exists - bump version
# 2. Invalid token - regenerate and update secret
# 3. Package name conflict - check PyPI for existing package

# Fix and retry manually:
python -m build
twine upload dist/*
```

### Tag Already Exists

```bash
# Delete local tag
git tag -d v0.2.0

# Delete remote tag
git push origin :refs/tags/v0.2.0

# Recreate tag
git tag -a v0.2.0 -m "Release v0.2.0"
git push origin v0.2.0
```

## Post-Release Tasks

After successful release:

1. **Update Documentation**
   - Ensure docs site is up to date
   - Update examples if API changed

2. **Announce Release**
   - Write blog post/announcement
   - Post to social media
   - Update Discord/Slack

3. **Start Next Version**
   - Create milestone for next version
   - Plan features for next release
   - Update roadmap if needed

## GitHub Secrets Setup

Required secrets in repository settings:

```
PYPI_API_TOKEN - PyPI API token for publishing
GITHUB_TOKEN   - Automatically provided by GitHub
```

### Setting up PyPI Token

1. Go to https://pypi.org/manage/account/token/
2. Click "Add API token"
3. Name: "Nexus GitHub Actions"
4. Scope: "Entire account" (or specific to nexus-ai-fs)
5. Copy token (starts with `pypi-`)
6. Add to GitHub: Settings → Secrets → Actions → New repository secret
7. Name: `PYPI_API_TOKEN`
8. Value: Paste the token

## Rollback

If a release has critical bugs:

```bash
# DO NOT delete releases from PyPI (violates policies)
# Instead, release a patch version with fixes

# Example: v0.2.0 has bug, release v0.2.1
git checkout v0.2.0
# Apply fixes
git commit -m "Fix critical bug in v0.2.0"
git tag -a v0.2.1 -m "Release v0.2.1 - Hotfix"
git push origin v0.2.1
```

## Support

For release issues:
- Check GitHub Actions logs
- Review PyPI upload errors
- Contact maintainers on Discord
- Create issue at https://github.com/nexi-lab/nexus/issues

---

Last updated: 2025-10-17
