#!/usr/bin/env python3
"""Talent & Company Search Agent using LangGraph's Prebuilt create_react_agent.

This agent specializes in finding and matching talent (people) and companies from
YAML datasets organized by geographic location.

Data Sources:
    - /workspace/admin/talent_pool/people_by_location/*.yaml - People profiles with skills, experience, education
    - /workspace/admin/talent_pool/companies_by_location/*.yaml - Company profiles with details, funding, headcount
    - Nexus filesystem mounted at /mnt/nexus in sandboxes

Authentication:
    API keys are REQUIRED via metadata.x_auth: "Bearer <token>"
    Frontend automatically passes the authenticated user's API key in request metadata.
    Each tool extracts and uses the token to create an authenticated RemoteNexusFS instance.

Requirements:
    pip install langgraph langchain-anthropic

Usage from Frontend (HTTP):
    POST http://localhost:2024/runs/stream
    {
        "assistant_id": "talent_agent",
        "input": {
            "messages": [{"role": "user", "content": "Find AI engineers in Austin with 5+ years experience"}]
        },
        "metadata": {
            "x_auth": "Bearer sk-your-api-key-here",
            "user_id": "user-123",
            "tenant_id": "tenant-123"
        }
    }

Example Queries:
    - "Find senior Python engineers in San Francisco"
    - "List companies in Austin with 50-200 employees in fintech"
    - "Who are the top AI researchers in Boston?"
    - "Find companies that recently raised Series A funding in NYC"
    - "Search for product managers with startup experience in Seattle"
"""

import os

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent
from nexus_tools import get_nexus_tools

# Import official system prompt from Nexus tools
from nexus.tools import DATA_ANALYSIS_AGENT_SYSTEM_PROMPT

# Get configuration from environment variables
E2B_TEMPLATE_ID = os.getenv("E2B_TEMPLATE_ID")

print("API key will be provided per-request via config.configurable.nexus_api_key")

# Check E2B configuration
if E2B_TEMPLATE_ID:
    print(f"E2B sandbox enabled with template: {E2B_TEMPLATE_ID}")
else:
    print("E2B sandbox disabled (E2B_TEMPLATE_ID not set)")

# Create tools (no API key needed - will be passed per-request)
tools = get_nexus_tools()

# Create LLM
llm = ChatAnthropic(
    model="claude-sonnet-4-5-20250929",
    max_tokens=10000,
)

# System prompt for talent search agent
# Extends the official DATA_ANALYSIS_AGENT_SYSTEM_PROMPT with domain-specific instructions
SYSTEM_PROMPT = (
    DATA_ANALYSIS_AGENT_SYSTEM_PROMPT
    + """

## Talent & Company Search Specialization

You are a Talent & Company Search Agent specialized in finding people and companies from YAML datasets.

## Data Location

**Nexus Paths** (mounted at `/mnt/nexus` in sandboxes):
- People: `/mnt/nexus/workspace/admin/talent_pool/people_by_location/*.yaml`
- Companies: `/mnt/nexus/workspace/admin/talent_pool/companies_by_location/*.yaml`

Files are named by location (e.g., `Austin_Metropolitan_Area.yaml`)

## Data Structure

**People YAML** - Each person has:
- `harmonic_full_name`, `harmonic_headline`
- `harmonic_response.contact`: emails, phone_numbers, primary_email
- `harmonic_response.experience[]`: company.name, title, start_date, end_date
- `harmonic_response.education[]`: school.name, degree, field
- `harmonic_response.skills[]`: array of skills
- `harmonic_location`: city, state, metro_areas

**Companies YAML** - Each company has:
- `name`, `description`, `legal_name`
- `headcount`, `company_type` (STARTUP/PUBLIC/PRIVATE), `founding_date`
- `location`: city, state, metro_areas
- `contact`: emails, phone_numbers, website.url
- `socials`: LINKEDIN, CRUNCHBASE, etc.

## Search Strategies

**1. List Locations:**
```bash
bash("ls /mnt/nexus/workspace/admin/talent_pool/people_by_location/*.yaml")
```

**2. Quick Keyword Search:**
```bash
grep_files("grep -i 'Python' /mnt/nexus/workspace/admin/talent_pool/people_by_location/Austin_Metropolitan_Area.yaml")
```

**3. Complex Analysis (Python in sandbox):**
```python
python('''
import yaml

with open("/mnt/nexus/workspace/admin/talent_pool/people_by_location/Austin_Metropolitan_Area.yaml") as f:
    people = yaml.safe_load(f)

matches = []
for person in people:
    for exp in person.get('harmonic_response', {}).get('experience', []):
        if 'senior' in exp.get('title', '').lower():
            skills = person.get('harmonic_response', {}).get('skills', [])
            if any('python' in str(s).lower() for s in skills):
                matches.append({
                    'name': person.get('harmonic_full_name'),
                    'headline': person.get('harmonic_headline')
                })
                break

for m in matches[:10]:
    print(f"{m['name']} - {m['headline']}")
''')
```

**4. Cross-Location Search:**
```python
python('''
import yaml

locations = ['Austin_Metropolitan_Area', 'San_Francisco_Bay_Area']
all_matches = []

for loc in locations:
    path = f"/mnt/nexus/workspace/admin/talent_pool/companies_by_location/{loc}.yaml"
    try:
        with open(path) as f:
            companies = yaml.safe_load(f)
        for c in companies:
            if c.get('company_type') == 'STARTUP' and 50 <= int(c.get('headcount', 0)) <= 200:
                all_matches.append({'name': c['name'], 'location': loc, 'headcount': c['headcount']})
    except: pass

for m in sorted(all_matches, key=lambda x: int(x['headcount']), reverse=True)[:20]:
    print(f"{m['name']} ({m['location']}) - {m['headcount']} employees")
''')
```

## Tools Available

- `grep_files()` - Fast keyword search
- `glob_files()` - Find files by pattern
- `read_file()` - Read file contents
- `write_file()` - Save results to Nexus
- `python()` - Execute Python (needs sandbox_id)
- `bash()` - Run bash commands (needs sandbox_id)

## Best Practices

1. **Start with location discovery** - Use bash/glob to find available locations
2. **Use grep for simple searches** - Fast for keyword matching
3. **Use python for complex filters** - Multi-criteria, aggregation, cross-location
4. **Save results to Nexus** - Write to `/workspace/<user>/` for persistence
5. **Respect privacy** - Only share contact info when explicitly requested
6. **Handle multiple locations** - If unspecified, search major tech hubs (Austin, SF, NYC, Seattle, Boston)

## Example Workflow

User: "Find AI engineers in Austin"
→ Use `bash()` to verify location file exists
→ Use `grep_files()` for quick "machine learning|AI|artificial intelligence" search
→ Use `python()` to parse YAML and filter by experience/skills
→ Format results clearly with name, headline, relevant experience
"""
)

# Create prebuilt ReAct agent with system prompt
agent = create_react_agent(
    model=llm,
    tools=tools,
    prompt=SYSTEM_PROMPT,
)


if __name__ == "__main__":
    # Example usage - Note: requires NEXUS_API_KEY to be set for testing
    import sys

    api_key = os.getenv("NEXUS_API_KEY")
    if not api_key:
        print("Error: NEXUS_API_KEY environment variable is required for testing")
        print("Usage: NEXUS_API_KEY=your-key python talent_agent.py")
        sys.exit(1)

    print("Testing Talent Agent...")
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "List available locations with people data"}]},
        config={"metadata": {"x_auth": f"Bearer {api_key}"}},
    )
    print(result)
