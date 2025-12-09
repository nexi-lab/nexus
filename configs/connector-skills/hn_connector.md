---
name: hackernews
description: Hacker News connector mounted at {mount_path}. Access HN stories, comments, and user profiles as JSON files.
---

- **Read-only**: Cannot write or delete (public API)
- **Virtual folders**: `top/`, `new/`, `best/`, `ask/`, `show/`, `jobs/` contain ranked story lists
- **Story files**: `1.json` to `10.json` (ranked position) with nested comments included
- **All content is JSON**
