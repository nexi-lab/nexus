# Google Calendar Connector

## Mount Path
`/mnt/calendar/`

## Overview
The Google Calendar connector provides file-based access to Google Calendar events. Each event is represented as a YAML file that can be read, created, updated, or deleted.

## Directory Structure
```
/mnt/calendar/
  primary/              # User's primary calendar
    event_id.yaml       # Individual event files
    _new.yaml           # Write here to create new events
  work@example.com/     # Secondary calendars by ID
    ...
```

## Operations

### Create Event

Write to `<calendar_id>/_new.yaml`:

```yaml
# agent_intent: <reason  for this operation>
summary: "Meeting Title"
start:
  dateTime: "2024-01-15T09:00:00-08:00"
  timeZone: America/Los_Angeles
end:
  dateTime: "2024-01-15T10:00:00-08:00"
  timeZone: America/Los_Angeles
description: "Event description"
location: "Conference Room A"
attendees:
  - email: attendee@example.com
    displayName: Attendee Name
reminders:
  - method: email
    minutes: 30
  - method: popup
    minutes: 10
recurrence:
  - "RRULE:FREQ=WEEKLY;BYDAY=MO"
visibility: default  # default, public, private, confidential
colorId: "1"  # 1-11
```

### Read Event

Read from `<calendar_id>/<event_id>.yaml`:

```bash
nexus cat /mnt/calendar/primary/abc123.yaml
```

### Update Event

Write to existing `<calendar_id>/<event_id>.yaml`:

```yaml
# agent_intent: <reason for this operation>
summary: "Updated Meeting Title"
description: "Updated description"
# Only include fields you want to change
```

### Delete Event

Delete requires explicit confirmation:

```yaml
# agent_intent: <reason for this operation>
# confirm: true
send_notifications: true
```

### List Events

```bash
nexus ls /mnt/calendar/primary/
```

## Required Format

All operations require `# agent_intent: <reason>` as the first line explaining why you're performing this action.

Operations requiring explicit confirmation (`delete_event`):
- Add `# confirm: true` after agent_intent

## DateTime Format

Use ISO 8601 format with timezone offset (RFC 3339):
- `2024-01-15T09:00:00-08:00` (with offset)
- `2024-01-15T09:00:00Z` (UTC)

TimeZone should be IANA format: `America/Los_Angeles`, `Europe/London`, `Asia/Tokyo`, etc.

## Recurrence Rules

Use RFC 5545 RRULE format:
- `RRULE:FREQ=DAILY` - Every day
- `RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR` - Mon, Wed, Fri
- `RRULE:FREQ=MONTHLY;BYMONTHDAY=1` - First of each month
- `RRULE:FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1` - January 1st

## Error Codes

### MISSING_AGENT_INTENT
Calendar operations require agent_intent explaining why you're performing this action.

**Fix:**
```yaml
# agent_intent: User requested to schedule a team meeting for Monday
```

### AGENT_INTENT_TOO_SHORT
agent_intent must be at least 10 characters to provide meaningful context.

**Fix:**
```yaml
# agent_intent: User asked to create weekly standup meeting with the team
```

### MISSING_CONFIRM
Delete operations require explicit confirmation.

**Fix:**
```yaml
# agent_intent: User wants to cancel the meeting
# confirm: true
```

### INVALID_DATETIME_FORMAT
Invalid datetime format. Use ISO 8601 with timezone offset.

**Fix:**
```yaml
start:
  dateTime: "2024-01-15T09:00:00-08:00"
  timeZone: America/Los_Angeles
```

### MISSING_REQUIRED_FIELD
Missing required field for this operation.

**Fix:**
```yaml
summary: Meeting Title
start:
  dateTime: "2024-01-15T09:00:00-08:00"
end:
  dateTime: "2024-01-15T10:00:00-08:00"
```

### END_BEFORE_START
Event end time must be after start time.

**Fix:**
```yaml
start:
  dateTime: "2024-01-15T09:00:00-08:00"
end:
  dateTime: "2024-01-15T10:00:00-08:00"  # Must be after start
```

### EVENT_NOT_FOUND
Event not found. It may have been deleted or you may not have access.

**Fix:**
```bash
# List events first to get valid event IDs:
nexus ls /mnt/calendar/primary/
```

### CALENDAR_NOT_FOUND
Calendar not found. Check the calendar ID.

**Fix:**
```bash
# Use 'primary' for the user's main calendar:
nexus ls /mnt/calendar/primary/
```

### PERMISSION_DENIED
You don't have permission to modify this event.

### QUOTA_EXCEEDED
Google Calendar API quota exceeded. Try again later.

## Examples

### Create a Team Meeting

```yaml
# agent_intent: User requested to schedule weekly team sync meeting
summary: Weekly Team Sync
description: Weekly sync to discuss project progress and blockers
start:
  dateTime: "2024-01-15T10:00:00-08:00"
  timeZone: America/Los_Angeles
end:
  dateTime: "2024-01-15T11:00:00-08:00"
  timeZone: America/Los_Angeles
location: Conference Room B
attendees:
  - email: alice@example.com
  - email: bob@example.com
recurrence:
  - "RRULE:FREQ=WEEKLY;BYDAY=MO"
reminders:
  - method: popup
    minutes: 15
```

### Update Event Title

```yaml
# agent_intent: User wants to rename the meeting to be more specific
summary: Q1 Planning - Weekly Team Sync
```

### Delete an Event

```yaml
# agent_intent: User requested to cancel the meeting as project is complete
# confirm: true
send_notifications: true
```
