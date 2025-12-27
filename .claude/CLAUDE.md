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

**IMPORTANT**: The deployment consists of TWO separate repositories:
- `~/nexus` - Main Nexus backend (this repo)
- `~/nexus-frontend` - Frontend UI (separate repo: nexi-lab/nexus-frontend)

Both must be updated for a complete deployment!

**Full production deployment (recommended):**
```bash
gcloud compute ssh nexus-server-spot --zone=us-west1-a --command="bash ~/nexus/scripts/deploy-production.sh"
```

**Quick deploy - Backend only (after PyPI release):**
```bash
gcloud compute ssh nexus-server-spot --zone=us-west1-a --command="cd ~/nexus && git pull && docker-compose -f docker-compose.demo.yml pull nexus && docker-compose -f docker-compose.demo.yml up -d nexus"
```

**Quick deploy - Frontend only (after nexus-frontend repo update):**
```bash
gcloud compute ssh nexus-server-spot --zone=us-west1-a --command="cd ~/nexus-frontend && git pull && cd ~/nexus && docker-compose -f docker-compose.demo.yml build frontend && docker-compose -f docker-compose.demo.yml up -d frontend"
```

**Verify:**
```bash
curl http://35.197.30.59:2026/health
gcloud compute ssh nexus-server-spot --zone=us-west1-a --command="docker exec nexus-server pip show nexus-ai-fs | grep Version"
```

**Server details:**
- Instance: `nexus-server-spot` (GCP Spot VM - e2-standard-2)
- IP: `35.197.30.59` (Static IP)
- Domain: `nexus.sudorouter.ai` (Caddy HTTPS reverse proxy)
- Deployment: Docker Compose (`docker-compose.demo.yml`)
- Repositories:
  - Backend: `~/nexus` (nexi-lab/nexus)
  - Frontend: `~/nexus-frontend` (nexi-lab/nexus-frontend)
- Endpoints:
  - Frontend: http://35.197.30.59:5173
  - API: http://35.197.30.59:2026
  - LangGraph: http://35.197.30.59:2024

**Rebuild frontend with new configuration:**
```bash
# If API URLs change, rebuild frontend
gcloud compute ssh nexus-server-spot --zone=us-west1-a --command="cd ~/nexus-frontend && docker build --build-arg VITE_NEXUS_API_URL=http://35.197.30.59:2026 --build-arg VITE_LANGGRAPH_API_URL=http://35.197.30.59:2024 -t nexus-frontend:latest . && cd ~/nexus && docker-compose -f docker-compose.demo.yml up -d frontend"
```

---

## AI Development Guidelines

**Role**: Senior Developer (AI). Human is PM, makes all decisions.

**Workflow**: Propose (2-3 options) → Approve → Implement → Show diff → Get commit approval

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
