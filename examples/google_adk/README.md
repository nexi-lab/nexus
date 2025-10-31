# Google ADK + Nexus Integration

This example shows how to integrate Nexus filesystem operations with Google ADK agents.

## Quick Start

### 1. Install Dependencies

```bash
pip install google-adk nexus-ai-fs
```

### 2. Get Google API Key

Get your Gemini API key at: https://makersuite.google.com/app/apikey

### 3. Run the Example

```bash
export GOOGLE_API_KEY="your-key-here"
python examples/google_adk/basic_adk_agent.py
```

Or use the helper script:
```bash
export GOOGLE_API_KEY="your-key-here"
bash RUN_GOOGLE_ADK.sh
```

## What It Does

The agent can:
- Search file content with `grep_files()`
- Find files by name with `glob_files()`
- Read file content with `read_file()`
- Write reports with `write_file()`

Example task: Find all Python files with async/await, analyze them, and write a summary report.

## How It Works

Google ADK requires:
1. **Agent** - LlmAgent with tools and instructions
2. **Session Service** - Manages conversation state (InMemorySessionService)
3. **Runner** - Executes agent with session management
4. **Session Creation** - Must create session before running

```python
from google.adk.agents import LlmAgent
from google.adk import Runner
from google.adk.sessions import InMemorySessionService, Session

# 1. Create agent with Nexus tools
agent = LlmAgent(
    name="file_agent",
    model="gemini-2.5-flash",
    instruction="You are a filesystem assistant",
    tools=[grep_files, read_file, write_file]
)

# 2. Create session service
session_service = InMemorySessionService()

# 3. Create runner
runner = Runner(
    app_name="my-app",
    agent=agent,
    session_service=session_service
)

# 4. Create session
session = Session(
    id="session-123",
    user_id="user-123",
    app_name="my-app"
)
session_service.create(session)

# 5. Run with formatted message
message = types.Content(
    role="user",
    parts=[types.Part(text="Find Python files")]
)

for event in runner.run(
    user_id="user-123",
    session_id="session-123",
    new_message=message
):
    # Process events...
    pass
```

## Comparison with LangGraph

Both frameworks integrate with Nexus the same way - wrap Nexus operations as Python functions and pass as tools.

**Key Differences:**

| Aspect | LangGraph | Google ADK |
|--------|-----------|------------|
| Setup | StateGraph + nodes | Agent + Runner + SessionService |
| API Style | Graph-based | Service-based |
| Session Management | Optional | Required |
| Best For | Complex workflows, multi-LLM | Production deployments, Google Cloud |

## Files

- `basic_adk_agent.py` - Main example (✅ working)
- `multi_agent_demo.py` - Multi-agent coordination (conceptual)
- `test_installation.py` - Verify setup
- `comparison_with_langgraph.md` - Detailed comparison

## Troubleshooting

### Import Errors

Google ADK uses namespace packages which can conflict in virtual environments. If you get import errors:

```bash
# Option 1: Use global Python
/opt/anaconda3/bin/python examples/google_adk/basic_adk_agent.py

# Option 2: Fresh conda environment
conda create -n adk python=3.12
conda activate adk
pip install google-adk nexus-ai-fs
```

### API Key Errors

Make sure GOOGLE_API_KEY is set:
```bash
export GOOGLE_API_KEY="your-key"
python examples/google_adk/basic_adk_agent.py
```

## Next Steps

- Try the working example: `python examples/google_adk/basic_adk_agent.py`
- Compare with LangGraph: `python examples/langgraph/langgraph_react_demo.py`
- Check Claude Agent SDK (simpler): `python examples/claude_agent_sdk/claude_agent_react_demo.py`

## Questions?

- Google ADK: https://github.com/google/adk-python
- Nexus: https://github.com/nexi-lab/nexus
