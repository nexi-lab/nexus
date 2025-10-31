# Nexus Session Onboarding

**Auto-execute on session start**

---

## Tasks

1. **Pull latest changes**
   ```bash
   git pull
   ```

2. **Check for handoff files**
   ```bash
   ls -la NEXT_SESSION_START.md SESSION_SUMMARY_*.md 2>/dev/null
   ```
   - If exist: Read them first

3. **Read project guidelines**
   - Read `.claude/CLAUDE.md`

4. **Check project status**
   ```bash
   gh issue list --state open --limit 5
   gh issue list --label in-progress
   git status
   git log -3 --oneline
   ```

5. **Ready**
   - Wait for PM to assign task
