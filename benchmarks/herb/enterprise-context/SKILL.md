---
name: herb-enterprise-context
description: Search enterprise context data (Slack, docs, meetings, PRs) from the HERB benchmark to answer questions about products, employees, customers, and organizational activities. Use when answering questions that require searching through enterprise communication and documentation.
allowed-tools: Read, Grep, Glob
---

# HERB Enterprise Context

Search through enterprise context data to answer questions about products, teams, customers, and organizational activities.

## Data Structure

```
enterprise-context/
├── SKILL.md                    # This file
├── _metadata/                  # Global reference data
│   ├── customers.jsonl         # 120 customers (CUST-ID → name, role, company)
│   ├── employees.jsonl         # 530 employees (eid_xxx → name, role, location, org)
│   ├── org_structure.jsonl     # Org hierarchy with reporting chains
│   └── org_structure.md        # Human-readable org chart
├── _summary.json               # Statistics for all products
└── {product}/                  # 30 products
    ├── _meta.json              # team[], customers[] for this product
    ├── slack/
    │   └── {channel}.jsonl     # Messages by channel (planning-*, develop-*, bug-*)
    ├── docs/
    │   ├── _index.jsonl        # Document metadata (id, type, author, date)
    │   └── {doc_id}.md         # Full document content
    ├── meetings/
    │   ├── _index.jsonl        # Meeting metadata (id, type, date, participants)
    │   ├── {id}.md             # Meeting transcripts
    │   └── {id}_chat.txt       # Meeting chat logs
    ├── prs/
    │   ├── _index.jsonl        # All PR metadata
    │   └── {repo}.jsonl        # PRs grouped by repository
    └── urls.jsonl              # Shared links
```

## Products (30 total)

ActionGenie, AnomalyForce, AutoTuneForce, CoachForce, CollaborateForce,
CollaborationForce, ConnectForce, ContentForce, ContextForce, EdgeForce,
ExplainabilityForce, FeedbackForce, FlowForce, ForecastForce, InsightForce,
KnowledgeForce, LeadForce, MonitorForce, PersonalizeForce, PitchForce,
ProposalForce, SearchFlow, SearchForce, SecurityForce, SentimentForce,
SummarizeForce, SupportForce, TrendForce, VizForce, WorkFlowGenie

---

## Search Strategies

### 1. Identify the Person (Employee/Customer Lookup)

When you find an employee ID (eid_xxx) or customer ID (CUST-xxx), look them up:

```bash
# Look up employee by ID
grep "eid_13fdff84" _metadata/employees.jsonl

# Look up customer by ID
grep "CUST-0096" _metadata/customers.jsonl

# Find employee's manager/reports
grep "eid_13fdff84" _metadata/org_structure.jsonl
```

### 2. Find Documents by Type or Author

```bash
# Find all Product Requirements Documents
grep "Product Requirements" {product}/docs/_index.jsonl

# Find documents by author
grep "eid_13fdff84" {product}/docs/_index.jsonl

# Read the full document
cat {product}/docs/{doc_id}.md
```

### 3. Search Slack Conversations

```bash
# Search for topic across all channels
grep -i "market research" {product}/slack/*.jsonl

# Find messages by user
grep "eid_13fdff84" {product}/slack/*.jsonl

# Search planning channels specifically
grep -i "keyword" {product}/slack/planning-*.jsonl
```

### 4. Find Meeting Participants

```bash
# Find meetings with specific participant
grep "eid_13fdff84" {product}/meetings/_index.jsonl

# Search meeting transcripts
grep -i "keyword" {product}/meetings/*.md
```

### 5. Search Pull Requests

```bash
# Find merged PRs
grep '"merged": true' {product}/prs/*.jsonl

# Find PRs by author
grep "eid_xxx" {product}/prs/_index.jsonl

# Search by repository
cat {product}/prs/{repo}.jsonl
```

### 6. Cross-Product Search

```bash
# Search across all products
grep -r "security" */docs/_index.jsonl

# Find employee across all data
grep -r "eid_13fdff84" **/*.jsonl
```

---

## Common Question Types

### Person Questions
- "Find employee IDs of authors/reviewers of [document]"
- "Who participated in [meeting]?"
- "Find the team members for [product]"

**Strategy**: Search docs/_index.jsonl, meetings/_index.jsonl, slack messages, then look up employee IDs in _metadata/employees.jsonl

### Document Questions
- "What does the [document type] say about [topic]?"
- "Find the latest version of [document]"

**Strategy**: Search docs/_index.jsonl for the document type, read the full .md file

### Communication Questions
- "What was discussed about [topic] in Slack?"
- "Find feedback on [document/feature]"

**Strategy**: Search slack/*.jsonl for keywords, follow thread replies

### Timeline Questions
- "When was [document] finalized?"
- "What happened after [event]?"

**Strategy**: Check timestamps in docs/_index.jsonl, slack messages, meetings/_index.jsonl

### Relationship Questions
- "Who reports to [person]?"
- "Which customers are associated with [product]?"

**Strategy**: Use _metadata/org_structure.jsonl for reporting, {product}/_meta.json for customers

---

## Data Formats

### JSONL Files (one record per line)

**employees.jsonl**:
```json
{"id": "eid_13fdff84", "name": "Charlie Davis", "role": "Marketing Research Analyst", "location": "Remote", "org": "slack"}
```

**Slack messages**:
```json
{"id": "20260611-0-a77ec", "user": "eid_13fdff84", "ts": "2026-06-11T20:31:00", "text": "Hi team, I've shared the Market Research Report..."}
```

**PR records**:
```json
{"id": "...", "number": 31, "title": "...", "user": "eid_xxx", "merged": true, "reviewers": ["eid_yyy"]}
```

### Markdown Documents

Documents have consistent headers:
```markdown
# Document Type

**ID:** doc_id | **Author:** eid_xxx | **Date:** 2026-06-10

---

[Content]
```

---

## Tips

1. **Start with _index.jsonl files** - They contain metadata for quick searching
2. **Use product _meta.json** - Shows team members and customers for that product
3. **Check multiple sources** - Information may be spread across docs, slack, and meetings
4. **Follow citations** - Message IDs and document IDs can be cross-referenced
5. **Verify with employee lookup** - Always resolve eid_xxx to get the person's name/role
