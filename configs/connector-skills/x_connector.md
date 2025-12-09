---
name: x
description: X (Twitter) connector mounted at {mount_path}. Access tweets, timeline, mentions, and post new tweets.
---

- **Virtual folders**: `timeline/`, `mentions/`, `posts/`, `bookmarks/`, `search/`, `users/`
- **Post tweet**: Write JSON to `posts/new.json` with `{"text": "your tweet"}`
- **Delete tweet**: Delete `posts/{tweet_id}.json` (own tweets only)
- **Rate limits**: X API has strict rate limits
- **Most paths read-only**
