# Connector Transport Matrix

Per-connector Transport implementation status. All 7 API connectors + 3 blob
storage transports implement the `Transport` protocol, composed with
`PathAddressingEngine` (or `CASAddressingEngine` for blob CAS backends).

## Transport Method Coverage

| Method | Gmail | GDrive | Slack | X | HN | Calendar | CLI |
|--------|-------|--------|-------|---|-----|----------|-----|
| `fetch` | messages.get → YAML | files.get + media_download | conversations.history → YAML | tweets.get → YAML | item/{id} → YAML | events.get → YAML | subprocess stdout |
| `store` | read-only | files.create / files.update | chat.postMessage | tweets.create | read-only | events.insert / events.update | subprocess stdin |
| `remove` | read-only | files.update(trashed=true) | read-only | tweets.delete | read-only | events.delete | subprocess |
| `exists` | messages.get(minimal) | files.list(q=name) | check | check | check | events.get | check |
| `get_size` | sizeEstimate | files.get(fields=size) | len(yaml) | len(yaml) | len(yaml) | len(yaml) | len(stdout) |
| `list_keys` | messages.list(labelIds) | files.list(q=parents) | conversations.list | timeline/mentions | topstories/new/best | calendarList + events.list | subprocess |
| `copy_key` | N/A | files.copy | N/A | N/A | N/A | N/A | N/A |
| `create_dir` | labels=virtual | folders.create | channels=virtual | N/A | N/A | calendars=virtual | N/A |
| `stream` | fetch+chunk | media_download chunked | fetch+chunk | fetch+chunk | fetch+chunk | fetch+chunk | streaming stdout |
| `store_chunked` | read-only | files.create(resumable) | N/A | N/A | read-only | join+store | subprocess stdin |

N/A methods raise `BackendError`. Read-only transports raise on `store`/`remove`.

## Per-Connector Details

### Gmail (PathGmailBackend + GmailTransport)

| | |
|---|---|
| **Auth** | OAuth 2.0 (Google), provider=`gmail` |
| **Path namespace** | `/{LABEL}/{thread_id}-{msg_id}.yaml` — labels: SENT, STARRED, IMPORTANT, INBOX |
| **Read/Write** | Read-only (email fetch). Send/reply/forward via write_content + trait validation |
| **Caching** | CacheConnectorMixin — IMMUTABLE_VERSION (emails never change) |
| **Batch** | Gmail batch API via `fetch_batch()` (50 messages/batch, exponential backoff) |
| **Rate limits** | Gmail API quota: 250 units/second/user |

### Google Drive (PathGDriveBackend + DriveTransport)

| | |
|---|---|
| **Auth** | OAuth 2.0 (Google), provider=`google-drive` |
| **Path namespace** | `/{folder}/{file.ext}` — direct path mapping under configurable root folder |
| **Read/Write** | Full CRUD. Google Workspace files auto-exported (Docs→DOCX, Sheets→XLSX, etc.) |
| **Caching** | No CacheConnectorMixin |
| **Folder ID cache** | In-memory `dict[str, str]` — path→Drive folder ID. Cache key includes user_id+zone_id |
| **Shared drives** | `use_shared_drives=True` + `shared_drive_id` adds corpora/driveId params |
| **Rate limits** | Drive API: 12,000 queries/minute/project |

### Google Calendar (PathCalendarBackend + CalendarTransport)

| | |
|---|---|
| **Auth** | OAuth 2.0 (Google), provider=`gcalendar` |
| **Path namespace** | `/{calendar_id}/{event_id}.yaml` — `primary/` = user's default calendar |
| **Read/Write** | Full CRUD. Write to `_new.yaml` = create, write to `{id}.yaml` = update |
| **Caching** | CacheConnectorMixin |
| **Validation** | Schema validation (CreateEventSchema, UpdateEventSchema, DeleteEventSchema) |
| **Checkpoints** | CheckpointMixin for reversible create/update/delete |
| **Rate limits** | Calendar API: 1,000,000 queries/day |

### Slack (PathSlackBackend + SlackTransport)

| | |
|---|---|
| **Auth** | OAuth 2.0 (Slack Bot Token), provider=`slack` |
| **Path namespace** | `/{channel_name}/{thread_ts}.yaml` — channels as virtual directories |
| **Read/Write** | Read messages + post via store(). No delete. |
| **Caching** | CacheConnectorMixin |
| **Rate limits** | Slack API tier-based: ~1 req/sec for most methods |

### X/Twitter (PathXBackend + XTransport)

| | |
|---|---|
| **Auth** | OAuth 2.0 PKCE (X), provider=`x` |
| **Path namespace** | `/{timeline\|mentions\|posts}/{tweet_id}.yaml` |
| **Read/Write** | Read timelines + create/delete tweets |
| **Caching** | Multi-tier (LRU + disk cache), NOT CacheConnectorMixin |
| **Rate limits** | X API v2 tier-dependent (Free: 500 tweets/month read) |

### Hacker News (PathHNBackend + HNTransport)

| | |
|---|---|
| **Auth** | None (public Firebase API) |
| **Path namespace** | `/{feed}/{story_id}.yaml` — feeds: top, new, best, ask, show, job |
| **Read/Write** | Read-only |
| **Caching** | CacheConnectorMixin |
| **Comments** | Recursive comment tree fetching via `_fetch_comments()` |
| **Rate limits** | Firebase: no official rate limit, be respectful |

### CLI (PathCLIBackend + CLITransport)

| | |
|---|---|
| **Auth** | Per-connector (env vars: GWS_ACCESS_TOKEN, GH_TOKEN, etc.) |
| **Path namespace** | Connector-defined via CLIConnectorConfig |
| **Read/Write** | Full CRUD via subprocess |
| **Subclasses** | GmailConnector, CalendarConnector, SheetsConnector, DocsConnector, ChatConnector, DriveConnector, GitHubConnector |
| **Sync** | CLISyncProvider + delta sync loop |
| **Rate limits** | Depends on underlying CLI tool |

## Gap Analysis: Beyond-Transport APIs

APIs that exist in external services but are NOT covered by the 10 Transport methods.
This is descriptive (design inventory), not prescriptive — no Transport extensions planned.

| Connector | Beyond-Transport APIs | Potential future extension |
|---|---|---|
| Gmail | labels, threads, drafts, send, search, filters | `search(query)`, `tag(key, label)` |
| GDrive | sharing/permissions, revisions, comments, export formats | `get_metadata(key)`, `share(key, user)` |
| Slack | reactions, threads, file uploads, user profiles, pins | `annotate(key, data)`, `get_thread(key)` |
| X | likes, retweets, followers, lists, spaces, polls | `react(key, type)`, `get_related(key)` |
| HN | comments (recursive), user karma, polls | `get_children(key)` |
| Calendar | attendees, recurrence, reminders, free/busy | `get_schedule(range)` |
| CLI | arbitrary commands, config, plugin system | `execute(command, args)` |

These capabilities are currently handled by connector-specific methods on the
PathXxxBackend (e.g., `_fetch_comments()` on PathHNBackend) or via write_content
with schema validation (e.g., send email via Gmail's trait-validated write path).
