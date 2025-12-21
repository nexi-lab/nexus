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
├── dataset/                    # Transformed context data (grep-friendly)
│   ├── _summary.json           # Overall statistics
│   ├── _metadata/              # Global reference data (JSONL)
│   └── {product}/              # Per-product data
└── qa/                         # Benchmark Q&A (evaluation)
    ├── answerable.jsonl        # 815 questions with ground truth
    └── unanswerable.jsonl      # 699 unanswerable questions
```

## Dataset Structure (Transformed)

```
dataset/
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
grep "eid_13fdff84" dataset/_metadata/employees.jsonl

# Find customers by company
grep "BlueWave" dataset/_metadata/customers.jsonl

# Find all VPs
grep '"role_type": "vp"' dataset/_metadata/org_structure.jsonl

# Find who reports to a specific lead
grep "eid_e96d2f38" dataset/_metadata/org_structure.jsonl
```

### Product Data Searches

```bash
# Find employee across all product data
grep "eid_13fdff84" dataset/ActionGenie/**/*.jsonl

# Search Slack messages
grep -i "market research" dataset/ActionGenie/slack/*.jsonl

# Find documents by type
grep "Product Requirements" dataset/ActionGenie/docs/_index.jsonl

# List all channels for a product
ls dataset/ActionGenie/slack/*.jsonl

# Find merged PRs
grep '"merged": true' dataset/ActionGenie/prs/*.jsonl

# Search across all products
grep -r "security" dataset/*/docs/_index.jsonl
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
python3 organize_context.py original/products/ActionGenie.json dataset/

# Transform all products in a directory
python3 organize_context.py --all original/products/ dataset/

# Transform metadata files
python3 organize_context.py --metadata original/metadata/ dataset/
```

## Data Format

- **Original**: Raw JSON files as provided by HERB benchmark
- **JSONL**: One complete JSON record per line (grep-friendly)
- **Markdown**: Human-readable content with headers
- **Index files**: Quick metadata lookup without parsing full content
- **Flattened structure**: Simple grep patterns work without JSON parsing

## Usage with Nexus

The `dataset/` folder is designed for AI agents to search using standard tools:
- `grep` for content search across JSONL files
- `glob` for file pattern matching
- Direct file reads for markdown documents
