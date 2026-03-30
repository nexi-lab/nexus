# Hacker News Connector

## Mount Path
`/mnt/hn/`

## Overview
The Hacker News connector provides read-only filesystem access to Hacker News stories and comments. Stories are organized by feed type (top, new, best, ask, show, jobs) and represented as JSON files with nested comments.

No authentication required - uses the public HN Firebase API.

## Directory Structure
```
/mnt/hn/
  top/                    # Top-ranked stories
    1.json ... 10.json    # Stories ranked 1-10
  new/                    # Newest submissions
    1.json ... 10.json
  best/                   # Algorithmically best stories
    1.json ... 10.json
  ask/                    # Ask HN posts (community questions)
    1.json ... 10.json
  show/                   # Show HN posts (project showcases)
    1.json ... 10.json
  jobs/                   # Job postings
    1.json ... 10.json
```

## Operations

### List Feeds

List available feed categories:

```bash
nexus ls /mnt/hn/
```

Returns:
```
top/
new/
best/
ask/
show/
jobs/
```

### List Stories in Feed

List stories in a specific feed:

```bash
nexus ls /mnt/hn/top/
```

Returns:
```
1.json
2.json
3.json
...
10.json
```

### Read Story

Read a story with its comments:

```bash
nexus cat /mnt/hn/top/1.json
```

Returns JSON with story metadata and nested comments:

```json
{
  "id": 12345678,
  "type": "story",
  "by": "username",
  "time": 1704067200,
  "title": "Show HN: My new project",
  "url": "https://example.com/project",
  "score": 142,
  "descendants": 87,
  "kids": [12345679, 12345680],
  "_rank": 1,
  "_feed": "top",
  "comments": [
    {
      "id": 12345679,
      "type": "comment",
      "by": "commenter1",
      "time": 1704067500,
      "text": "Great project! I especially like...",
      "parent": 12345678,
      "replies": [
        {
          "id": 12345681,
          "type": "comment",
          "by": "author",
          "text": "Thanks! We spent a lot of time on...",
          "replies": []
        }
      ]
    }
  ]
}
```

### Search Stories

Search across cached stories:

```bash
nexus grep "machine learning" /mnt/hn/top/
nexus grep "Show HN" /mnt/hn/
```

## Feed Types

| Feed | Description | Update Frequency |
|------|-------------|------------------|
| `top` | Top-ranked stories by score | ~5 minutes |
| `new` | Newest submissions | ~1 minute |
| `best` | Algorithmically selected best | ~1 hour |
| `ask` | "Ask HN" community questions | ~5 minutes |
| `show` | "Show HN" project showcases | ~5 minutes |
| `jobs` | Y Combinator job postings | ~1 hour |

## Story Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | int | Unique story identifier |
| `type` | string | "story", "job", "poll" |
| `by` | string | Author username |
| `time` | int | Unix timestamp |
| `title` | string | Story title |
| `url` | string | Link URL (if link post) |
| `text` | string | Post text (if text post) |
| `score` | int | Upvote score |
| `descendants` | int | Total comment count |
| `kids` | int[] | Direct child comment IDs |
| `_rank` | int | Position in feed (1-10) |
| `_feed` | string | Feed name (top/new/etc) |
| `comments` | array | Nested comment objects |

## Comment Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | int | Unique comment identifier |
| `type` | string | Always "comment" |
| `by` | string | Author username |
| `time` | int | Unix timestamp |
| `text` | string | Comment text (HTML) |
| `parent` | int | Parent story/comment ID |
| `dead` | bool | True if flagged/dead |
| `deleted` | bool | True if deleted |
| `replies` | array | Nested reply comments |

## Examples

### Get Today's Top Story

```bash
nexus cat /mnt/hn/top/1.json | jq '.title, .url, .score'
```

### Find AI-Related Stories

```bash
nexus grep -i "artificial intelligence\|AI\|GPT\|LLM" /mnt/hn/top/
```

### Get Ask HN Discussions

```bash
nexus cat /mnt/hn/ask/1.json | jq '.title, .text'
```

### Browse Job Postings

```bash
for i in 1 2 3; do
  nexus cat /mnt/hn/jobs/$i.json | jq -r '.title'
done
```

### Find Stories by Author

```bash
nexus grep '"by": "dang"' /mnt/hn/
```

## Caching

Stories are cached with TTL based on feed type:

| Feed | Cache TTL | Reason |
|------|-----------|--------|
| `new` | 1 minute | Changes very frequently |
| `top` | 5 minutes | Moderately dynamic |
| `ask` | 5 minutes | Moderately dynamic |
| `show` | 5 minutes | Moderately dynamic |
| `best` | 1 hour | Relatively stable |
| `jobs` | 1 hour | Changes slowly |

Use `sync` to pre-fetch and cache all stories:

```bash
nexus sync /mnt/hn/
nexus sync /mnt/hn/top/  # Sync specific feed
```

## Limitations

- **Read-only**: HN API does not support posting or voting
- **10 stories per feed**: Configurable up to 30 via connector settings
- **Comment depth limit**: Max 5 levels deep, 100 comments total
- **No search API**: Search is local (grep on cached content only)
- **No user profiles**: Cannot fetch user details directly
- **External content not included**: Only URLs, not article content

## HN Terminology

| Term | Meaning |
|------|---------|
| **karma** | User reputation score from upvotes |
| **Ask HN** | Questions posed to the community |
| **Show HN** | Project/creation showcases |
| **dead** | Flagged/hidden by moderators |
| **dang** | HN moderator (Daniel Gackle) |
| **pg** | Paul Graham (YC founder) |
| **flagged** | Reported by users |
| **[dupe]** | Duplicate submission |

## Rate Limits

The HN Firebase API has no documented rate limits, but the connector implements reasonable caching to avoid excessive requests.

## Related Resources

- [Hacker News](https://news.ycombinator.com)
- [HN API Documentation](https://github.com/HackerNews/API)
- [HN Search (Algolia)](https://hn.algolia.com)
