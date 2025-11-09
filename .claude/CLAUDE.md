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
gcloud compute ssh nexus-server-spot --zone=us-west1-a --command="cd ~/nexus && git pull && docker-compose -f docker-compose.demo.yml pull nexus && docker-compose -f docker-compose.demo.yml up -d nexus"
```

**Verify:**
```bash
curl http://35.197.30.59:8080/health
gcloud compute ssh nexus-server-spot --zone=us-west1-a --command="docker exec nexus-server pip show nexus-ai-fs | grep Version"
```

**Server details:**
- Instance: `nexus-server-spot` (GCP Spot VM - e2-standard-2)
- IP: `35.197.30.59` (Static IP)
- Domain: `nexus.sudorouter.ai` (Caddy HTTPS reverse proxy)
- Deployment: Docker Compose (`docker-compose.demo.yml`)
- Location: `~/nexus` (user: `songym`)
- Frontend: http://35.197.30.59:5173
- API: http://35.197.30.59:8080
- LangGraph: http://35.197.30.59:2024

**Rebuild frontend with new configuration:**
```bash
# If API URLs change, rebuild frontend
gcloud compute ssh nexus-server-spot --zone=us-west1-a --command="cd ~/nexus-frontend && docker build --build-arg VITE_NEXUS_API_URL=http://35.197.30.59:8080 --build-arg VITE_LANGGRAPH_API_URL=http://35.197.30.59:2024 -t nexus-frontend:latest . && cd ~/nexus && docker-compose -f docker-compose.demo.yml up -d frontend"
```

---

## AI Development Guidelines

**Role**: Senior Developer (AI). Human is PM, makes all decisions.

**Workflow**: Propose (2-3 options) → Approve → Implement → Show diff → Get commit approval

### Session Startup Protocol

**Every session:**
1. Pull latest changes (CRITICAL first step)
2. Find session handoff: Check top 10 recently updated issues (show number, time, title, labels). Read latest comments for "🔄 Session Handoff" marker. Prioritize `in-progress` labeled issues.
3. Check git status
4. Wait for PM to assign task

### Critical Rules

- **Issues must complete in single session** (<6h work)
- **If issue >6h**: Stop, alert PM, propose split

### Quality Checklist

Before closing any issue:
- [ ] Code tested, all tests pass
- [ ] Docs updated (if applicable)
- [ ] Diff reviewed by PM
- [ ] Commit approved by PM

### Commit Format

| Format | Example |
|--------|---------|
| `<type>(#issue): description` | `feat(#123): Add feature X` |

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`
**Always reference issue number.**

### Mobile Collaboration

Check `[MOBILE]` issues (label: `mobile-task`). Work in issue thread: post findings as comments, update label to `mobile-done-pc-todo` when done.