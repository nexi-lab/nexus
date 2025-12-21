# HERB Benchmark Data for Nexus

This directory contains the HERB (Heterogeneous Enterprise Retrieval Benchmark) data
for use with Nexus hub's file system, optimized for grep/glob search by AI agents.

## Source

Data sourced from: https://github.com/SalesforceAIResearch/HERB/tree/main/data/

## Directory Structure

```
benchmarks/herb/
├── README.md                   # This file
├── organize_context.py         # Transformation script
├── original/                   # Original HERB data (raw JSON)
│   ├── products/               # 30 product JSON files
│   │   └── {Product}.json      # Raw product data with all fields
│   └── metadata/               # Reference data
│       ├── customers_data.json # Customer information
│       ├── employee.json       # Employee directory
│       └── salesforce_team.json# Org structure hierarchy
├── enterprise-context/         # Transformed context data (skill package)
│   ├── SKILL.md                # Skill definition for agents
│   ├── _summary.json           # Overall statistics
│   ├── _metadata/              # Global reference data (JSONL)
│   └── {product}/              # Per-product data
└── qa/                         # Benchmark Q&A (evaluation)
    ├── answerable.jsonl        # 815 questions with ground truth
    └── unanswerable.jsonl      # 699 unanswerable questions
```

## Enterprise Context Structure

```
enterprise-context/
├── _summary.json               # Overall statistics for all products
├── _metadata/                  # Global reference data
│   ├── customers.jsonl         # Customer info (CUST-ID → name, role, company)
│   ├── employees.jsonl         # Employee info (eid_xxx → name, role, location)
│   ├── org_structure.jsonl     # Flattened org hierarchy with reporting chains
│   └── org_structure.md        # Human-readable org chart
├── {product}/
│   ├── _meta.json              # team[], customers[]
│   ├── slack/
│   │   └── {channel}.jsonl     # Slack messages by channel
│   ├── docs/
│   │   ├── _index.jsonl        # Document metadata
│   │   └── {doc_id}.md         # Document content
│   ├── meetings/
│   │   ├── _index.jsonl        # Meeting metadata
│   │   ├── {id}.md             # Transcripts
│   │   └── {id}_chat.txt       # Chat logs
│   ├── prs/
│   │   ├── _index.jsonl        # All PR metadata
│   │   └── {repo}.jsonl        # PRs by repository
│   └── urls.jsonl              # Shared links
```

## Data Summary

- **Products**: 30
- **Customers**: 120
- **Employees**: 530
- **Org Structure Members**: 530 (with reporting hierarchy)
- **Answerable Questions**: 815 (with ground truth and citations)
- **Unanswerable Questions**: 699

## Products

ActionGenie, AnomalyForce, AutoTuneForce, CoachForce, CollaborateForce,
CollaborationForce, ConnectForce, ContentForce, ContextForce, EdgeForce,
ExplainabilityForce, FeedbackForce, FlowForce, ForecastForce, InsightForce,
KnowledgeForce, LeadForce, MonitorForce, PersonalizeForce, PitchForce,
ProposalForce, SearchFlow, SearchForce, SecurityForce, SentimentForce,
SummarizeForce, SupportForce, TrendForce, VizForce, WorkFlowGenie

## Search Examples

### Metadata Searches

```bash
# Look up employee by ID
grep "eid_13fdff84" enterprise-context/_metadata/employees.jsonl

# Find customers by company
grep "BlueWave" enterprise-context/_metadata/customers.jsonl

# Find all VPs
grep '"role_type": "vp"' enterprise-context/_metadata/org_structure.jsonl

# Find who reports to a specific lead
grep "eid_e96d2f38" enterprise-context/_metadata/org_structure.jsonl
```

### Product Data Searches

```bash
# Find employee across all product data
grep "eid_13fdff84" enterprise-context/ActionGenie/**/*.jsonl

# Search Slack messages
grep -i "market research" enterprise-context/ActionGenie/slack/*.jsonl

# Find documents by type
grep "Product Requirements" enterprise-context/ActionGenie/docs/_index.jsonl

# List all channels for a product
ls enterprise-context/ActionGenie/slack/*.jsonl

# Find merged PRs
grep '"merged": true' enterprise-context/ActionGenie/prs/*.jsonl

# Search across all products
grep -r "security" enterprise-context/*/docs/_index.jsonl
```

### Q&A Benchmark Searches

```bash
# Find questions for a specific product
grep '"product": "ActionGenie"' qa/answerable.jsonl

# Find questions by type
grep '"type": "person"' qa/answerable.jsonl

# Count questions per product
grep -o '"product": "[^"]*"' qa/answerable.jsonl | sort | uniq -c
```

## Transformation Script

Use `organize_context.py` to regenerate the transformed dataset from original data:

```bash
# Transform a single product
python3 organize_context.py original/products/ActionGenie.json enterprise-context/

# Transform all products in a directory
python3 organize_context.py --all original/products/ enterprise-context/

# Transform metadata files
python3 organize_context.py --metadata original/metadata/ enterprise-context/
```

## Data Format

- **Original**: Raw JSON files as provided by HERB benchmark
- **JSONL**: One complete JSON record per line (grep-friendly)
- **Markdown**: Human-readable content with headers
- **Index files**: Quick metadata lookup without parsing full content
- **Flattened structure**: Simple grep patterns work without JSON parsing

## Usage with Nexus

The `enterprise-context/` folder is a skill package designed for AI agents. It includes:
- `SKILL.md` with search strategies and data format documentation
- JSONL files for grep-friendly content search
- Markdown documents for direct file reads
- `_index.jsonl` files for quick metadata lookup
