# MCP Server Evaluation Tests

LLM-driven evaluation tests for the Nexus MCP server. These tests verify that AI agents can effectively use the MCP tools to accomplish real-world tasks.

## Why Separate from Regular Tests?

These tests are **excluded from automatic test runs** because they:

1. **Require API Key**: Need `ANTHROPIC_API_KEY` environment variable
2. **Cost Money**: Consume LLM tokens on each run
3. **Are Slower**: Take longer than unit/integration tests
4. **Test Different Things**: Verify AI usability, not just code correctness

## Quick Start

### 1. Set API Key

```bash
export ANTHROPIC_API_KEY=your_api_key_here
```

### 2. Run Evaluation

```bash
# Using the Python script directly
cd tests/evaluation
python run_evaluation.py mcp_evaluation.xml

# Or with output file
python run_evaluation.py mcp_evaluation.xml --output report.md

# With custom model
python run_evaluation.py mcp_evaluation.xml --model claude-sonnet-4-20250514
```

## Directory Structure

```
tests/evaluation/
├── README.md              # This file
├── __init__.py            # Module marker
├── conftest.py            # Pytest configuration (skip by default)
├── mcp_evaluation.xml     # Evaluation questions
└── run_evaluation.py      # Evaluation runner script
```

## Creating New Evaluation Questions

### Guidelines

1. **Read-only**: Questions should only require non-destructive operations
2. **Stable answers**: Answers shouldn't change over time
3. **Complex**: Require multiple tool calls (not just one simple lookup)
4. **Realistic**: Based on tasks humans would actually want to accomplish
5. **Verifiable**: Single, clear answer that can be string-compared

### XML Format

```xml
<evaluation>
    <qa_pair>
        <question>Your complex question here?</question>
        <answer>Expected answer</answer>
    </qa_pair>
</evaluation>
```

### Example Questions

**Good** (complex, requires multiple steps):
```xml
<qa_pair>
    <question>Find all Python files that define a class with "Manager" in the name. Which file has the most such classes?</question>
    <answer>registry.py</answer>
</qa_pair>
```

**Bad** (too simple, answer might change):
```xml
<qa_pair>
    <question>How many files are in the workspace?</question>
    <answer>42</answer>
</qa_pair>
```

## Using mcp-builder Skill

For comprehensive evaluation creation, use the `mcp-builder` skill:

```
# In Claude Code, the skill is available at:
.claude/skills/mcp-builder/

# Key reference files:
- skill.md              # Main workflow guide
- reference/evaluation.md   # Evaluation creation guide
```

The mcp-builder skill provides a 4-phase approach:
1. Deep Research and Planning
2. Implementation
3. Review and Test
4. Create Evaluations

## Integration with CI/CD

These tests are **intentionally excluded** from CI pipelines:

- `pyproject.toml` has `--ignore=tests/evaluation` in pytest addopts
- `conftest.py` skips tests unless explicitly requested
- The `evaluation` marker can be used for filtering

To run in CI (e.g., scheduled nightly job):

```yaml
# Example GitHub Actions
- name: Run MCP Evaluations
  if: github.event_name == 'schedule'  # Only on scheduled runs
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  run: |
    cd tests/evaluation
    python run_evaluation.py mcp_evaluation.xml --output evaluation_report.md
```

## Troubleshooting

### "ANTHROPIC_API_KEY not set"

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### Tests are skipped when running pytest

This is intentional. Use the Python script directly:

```bash
python tests/evaluation/run_evaluation.py tests/evaluation/mcp_evaluation.xml
```

### Low accuracy in evaluations

Review the report to understand:
- Are tool descriptions clear enough?
- Is the returned data too verbose?
- Are error messages actionable?

Use insights to improve the MCP server implementation.
