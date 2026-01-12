# Gmail Connector

## Mount Path
`/mnt/gmail/`

## Overview
The Gmail connector provides file-based access to Gmail emails. Emails are organized by Gmail labels (INBOX, SENT, STARRED, IMPORTANT) and represented as YAML files that can be read, searched, and composed.

## Directory Structure
```
/mnt/gmail/
  SENT/                           # Sent emails (priority 1)
    {thread_id}-{msg_id}.yaml     # Individual email files
  STARRED/                        # Starred inbox emails (priority 2)
    {thread_id}-{msg_id}.yaml
  IMPORTANT/                      # Important inbox emails (priority 3)
    {thread_id}-{msg_id}.yaml
  INBOX/                          # Remaining inbox emails (priority 4)
    {thread_id}-{msg_id}.yaml
  DRAFTS/                         # Draft emails
    _new.yaml                     # Write here to create a draft
  .skill/                         # Skill documentation
    SKILL.md
    examples/
```

Note: Each email appears in exactly ONE folder based on highest priority label match.

## Operations

### Read Email

Read from `<label>/<thread_id>-<msg_id>.yaml`:

```bash
nexus cat /mnt/gmail/INBOX/abc123-xyz789.yaml
```

Returns email metadata and content in YAML format:
```yaml
id: xyz789
threadId: abc123
subject: Weekly Project Update
from: alice@example.com
to: bob@example.com
date: "2024-01-15T09:00:00Z"
body_text: |
  Hi Bob,

  Here's the weekly update...
snippet: "Hi Bob, Here's the weekly update..."
labelIds:
  - INBOX
  - UNREAD
```

### List Emails

```bash
nexus ls /mnt/gmail/INBOX/
nexus ls /mnt/gmail/SENT/
```

### Search Emails

```bash
nexus grep "project update" /mnt/gmail/INBOX/
nexus grep "from: alice@" /mnt/gmail/
```

### Send Email

Write to `SENT/_new.yaml`:

```yaml
# agent_intent: User requested to send project update to the team
to:
  - alice@example.com
  - bob@example.com
cc:
  - manager@example.com
subject: Weekly Project Update
body: |
  Hi team,

  Here's the weekly update on Project X:

  1. Completed the design review
  2. Started implementation phase
  3. Next milestone: March 15

  Best regards
priority: normal  # normal, high, low (optional)
confirm: true
```

### Reply to Email

Write to `SENT/_reply.yaml`:

```yaml
# agent_intent: User wants to reply to the project thread
thread_id: "18c1234567890abc"
message_id: "18c1234567890xyz"
body: |
  Thanks for the update!

  I've reviewed the docs and have some feedback:
  - The timeline looks good
  - Let's schedule a sync for next week

  Best,
reply_all: true
confirm: true
```

### Forward Email

Write to `SENT/_forward.yaml`:

```yaml
# agent_intent: User wants to forward the report to external partner
message_id: "18c1234567890abc"
to:
  - partner@external.com
cc:
  - manager@example.com
comment: |
  FYI - Here's the report we discussed.
include_attachments: true
confirm: true
```

### Create Draft

Write to `DRAFTS/_new.yaml`:

```yaml
# agent_intent: User wants to draft a response for later review
to:
  - client@example.com
subject: Re: Project Proposal
body: |
  Dear Client,

  Thank you for your proposal. We have reviewed it and...

  [Draft - will complete later]
thread_id: "18c1234567890abc"  # Optional: for reply drafts
```

Note: Drafts don't require `confirm: true` since they're not sent.

### With Attachments

```yaml
# agent_intent: User requested to send email with report attachment
to:
  - alice@example.com
subject: Q4 Report Attached
body: |
  Hi Alice,

  Please find the Q4 report attached.

  Best regards
attachments:
  - path: /mnt/storage/reports/q4-report.pdf
    filename: Q4-Report-2024.pdf  # Optional: override filename
    content_type: application/pdf  # Optional: auto-detected if not set
confirm: true
```

## Required Format

All write operations require `# agent_intent: <reason>` as the first line explaining why you're performing this action.

Operations requiring explicit confirmation (`send_email`, `reply_email`, `forward_email`):
- Add `confirm: true` to confirm the email should be sent
- **These actions CANNOT be undone** - sent emails cannot be recalled

Operations NOT requiring confirmation:
- `create_draft` - Drafts can be edited or deleted later

## Email Address Format

Email addresses must be valid RFC 5322 format:
- `user@example.com` (plain email)
- Addresses are automatically normalized to lowercase

## Error Codes

### MISSING_AGENT_INTENT
Email operations require agent_intent explaining why you're performing this action.

**Fix:**
```yaml
# agent_intent: User requested to send project update to the team
```

### AGENT_INTENT_TOO_SHORT
agent_intent must be at least 10 characters to provide meaningful context.

**Fix:**
```yaml
# agent_intent: Sending weekly status report as requested by user
```

### MISSING_CONFIRM
Sending emails requires explicit confirmation with `confirm: true`.

**Fix:**
```yaml
# agent_intent: User wants to send meeting notes
to:
  - alice@example.com
subject: Meeting Notes
body: |
  Here are the notes...
confirm: true  # Add this to confirm email should be sent
```

### MISSING_RECIPIENTS
Email must have at least one recipient in the `to` field.

**Fix:**
```yaml
to:
  - recipient@example.com
```

### INVALID_EMAIL_ADDRESS
One or more email addresses are invalid.

**Fix:**
```yaml
to:
  - valid@example.com  # Use proper email format
```

### MISSING_SUBJECT
Email subject is required.

**Fix:**
```yaml
subject: Meeting Follow-up
```

### MISSING_BODY
Email body is required.

**Fix:**
```yaml
body: |
  Hello,

  Here is the content...
```

### THREAD_NOT_FOUND
The specified thread_id does not exist or is not accessible.

**Fix:**
```yaml
thread_id: '18c1234567890abc'  # Use a valid thread ID from email listing
```

### MESSAGE_NOT_FOUND
The specified message_id does not exist or is not accessible.

**Fix:**
```yaml
message_id: '18c1234567890abc'  # Use a valid message ID
```

### ATTACHMENT_NOT_FOUND
Attachment file not found at specified path.

**Fix:**
```yaml
attachments:
  - path: /mnt/storage/report.pdf  # Ensure file exists
```

### ATTACHMENT_TOO_LARGE
Attachment exceeds Gmail's 25MB size limit.

**Fix:**
```yaml
# Use Google Drive for larger files
# Or split into multiple smaller attachments
```

### QUOTA_EXCEEDED
Gmail API quota exceeded - please wait before sending more emails.

**Fix:**
```yaml
# Wait a few minutes and try again
```

### OAUTH_TOKEN_EXPIRED
OAuth token has expired - re-authentication required.

**Fix:**
```bash
# Run: nexus oauth login gmail
```

### EXTERNAL_RECIPIENT_WARNING
Email contains external recipients (outside your organization). This is a warning - ensure external sharing is intended.

## Examples

### Send a Team Update

```yaml
# agent_intent: User requested to send weekly project update to the team
to:
  - alice@example.com
  - bob@example.com
cc:
  - manager@example.com
subject: Weekly Project Update - Week 3
body: |
  Hi team,

  Here's the weekly update on Project X:

  Completed:
  - Design review finished
  - API specifications approved

  In Progress:
  - Backend implementation (60% complete)
  - Frontend scaffolding

  Next Week:
  - Complete backend core features
  - Start integration testing

  Blockers: None

  Best regards
confirm: true
```

### Reply to a Thread

```yaml
# agent_intent: User wants to reply with feedback on the proposal
thread_id: "18c1234567890abc"
message_id: "18c1234567890xyz"
body: |
  Thanks for sharing the proposal!

  I've reviewed it and have a few suggestions:

  1. Consider adding a timeline section
  2. The budget breakdown looks good
  3. Can we discuss the resource allocation?

  Let me know when you're free to chat.

  Best,
reply_all: false
confirm: true
```

### Forward an Email

```yaml
# agent_intent: User wants to share the customer feedback with the product team
message_id: "18c1234567890abc"
to:
  - product-team@example.com
comment: |
  FYI - Important customer feedback below.

  We should discuss this in our next sprint planning.
include_attachments: true
confirm: true
```

### Create a Draft for Later

```yaml
# agent_intent: User wants to draft a response to review before sending
to:
  - client@example.com
subject: Re: Contract Renewal
body: |
  Dear Client,

  Thank you for reaching out about the contract renewal.

  [TODO: Add renewal terms]
  [TODO: Confirm pricing with finance]

  Best regards
thread_id: "18c1234567890abc"
```

### Search and Reply Workflow

```bash
# 1. Search for relevant emails
nexus grep "budget proposal" /mnt/gmail/INBOX/

# 2. Read the email
nexus cat /mnt/gmail/INBOX/abc123-xyz789.yaml

# 3. Reply to it
nexus write /mnt/gmail/SENT/_reply.yaml << 'EOF'
# agent_intent: User wants to approve the budget proposal
thread_id: "abc123"
message_id: "xyz789"
body: |
  Hi,

  I've reviewed the budget proposal and approve it as submitted.

  Please proceed with the next steps.

  Thanks,
confirm: true
EOF
```
