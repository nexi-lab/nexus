# Claude Development Guidelines

## Pull Request Workflow

**IMPORTANT:** Always create a feature branch and submit a PR before merging to main.

```bash
# Create a new feature branch
git checkout -b feature/your-feature-name

# Make changes, commit, and push
git add .
git commit -m "Your commit message"
git push origin feature/your-feature-name

# Create PR
gh pr create --title "Your PR title" --body "Description of changes"

# Wait for CI checks to pass before merging
gh pr checks
```

**Never push directly to main.** All changes must go through PR review and CI checks.

## Releasing to PyPI

1. Update version in `pyproject.toml`
2. Build: `/opt/homebrew/bin/python3.11 -m build`
3. Upload: `/opt/homebrew/bin/python3.11 -m twine upload -u __token__ -p "pypi-xxxxx" dist/*`
4. Tag: `git tag v0.x.x && git push origin v0.x.x`
5. Create PR for version bump

## Deploying to nexus-server (GCP)

**Quick deploy after PyPI release:**
```bash
gcloud compute ssh nexus-server --zone=us-west1-a --command="sudo -u nexus bash -c 'cd /opt/nexus/repo && git pull && /opt/nexus/repo/.venv/bin/pip install --upgrade nexus-ai-fs && sudo pkill -f \"nexus.cli serve\" && nohup /opt/nexus/repo/.venv/bin/python -m nexus.cli serve --host 0.0.0.0 --port 8080 --data-dir /var/lib/nexus > /tmp/nexus.log 2>&1 &'"
```

**Verify:**
```bash
curl http://35.230.4.67:8080/health
gcloud compute ssh nexus-server --zone=us-west1-a --command="sudo -u nexus /opt/nexus/repo/.venv/bin/pip show nexus-ai-fs | grep Version"
```

**Server details:**
- IP: `35.230.4.67`
- Domain: `nexus.sudorouter.ai` (Caddy HTTPS reverse proxy)
- Location: `/opt/nexus/repo` (user: `nexus`)
- Python: `python3.11` in `.venv`

---

## AI Development Guidelines

**Role**: Senior Developer (AI). Human is PM, makes all decisions.

**Workflow**: Propose (2-3 options) → Approve → Implement → Show diff → Get commit approval

**Critical rules**:
- Issues must complete in single session (<6h work, 200K context limit)
- If issue >6h: Stop, alert PM, propose split
- Session startup: git pull → check handoffs (NEXT_SESSION_START.md) → check issues → wait for PM
- Quality checklist before closing: tests pass, docs updated, diff reviewed, commit approved

**Mobile collaboration**: Check `[MOBILE]` issues if using Github Mobile.
