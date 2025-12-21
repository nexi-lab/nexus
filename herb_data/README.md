# HERB Benchmark Data for Nexus

This directory contains the HERB (Heterogeneous Enterprise Retrieval Benchmark) data
transformed for use with Nexus hub's file system, optimized for grep/glob search by AI agents.

## Source

Data sourced from: https://github.com/SalesforceAIResearch/HERB/tree/main/data/

## Structure

```
herb_contexts/
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
grep "eid_13fdff84" _metadata/employees.jsonl

# Find customers by company
grep "BlueWave" _metadata/customers.jsonl

# Find all VPs
grep '"role_type": "vp"' _metadata/org_structure.jsonl

# Find who reports to a specific lead
grep "eid_e96d2f38" _metadata/org_structure.jsonl
```

### Product Data Searches

```bash
# Find employee across all product data
grep "eid_13fdff84" ActionGenie/**/*.jsonl

# Search Slack messages
grep -i "market research" ActionGenie/slack/*.jsonl

# Find documents by type
grep "Product Requirements" ActionGenie/docs/_index.jsonl

# List all channels for a product
ls ActionGenie/slack/*.jsonl

# Find merged PRs
grep '"merged": true' ActionGenie/prs/*.jsonl

# Search across all products
grep -r "security" */docs/_index.jsonl
```

## Transformation Script

Use `organize_context.py` to regenerate the data or transform new products:

```bash
# Transform a single product
python3 organize_context.py Product.json output/

# Transform all products in a directory
python3 organize_context.py --all input_dir/ output/

# Transform metadata files
python3 organize_context.py --metadata metadata_dir/ output/
```

## Data Format

- **JSONL**: One complete JSON record per line (grep-friendly)
- **Markdown**: Human-readable content with headers
- **Index files**: Quick metadata lookup without parsing full content
- **Flattened structure**: Simple grep patterns work without JSON parsing
