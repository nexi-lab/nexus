# ACP — Coding Agents Tutorial

ACP (Agent Communication Protocol) lets you call coding agents — Claude Code,
Codex CLI, Gemini CLI, and others — through a unified JSON-RPC interface exposed
by nexusd.

This tutorial walks through the full ACP CLI: listing agents, calling them,
managing system prompts and skills, monitoring processes, and reviewing history.

## Prerequisites

- nexusd running with gRPC enabled
- At least one agent binary on `PATH` (`claude`, `codex`, or `gemini`)

```bash
# Start nexusd (gRPC enabled on port 2028 by default)
nexusd --port 2026

# In another terminal
export NEXUS_URL=http://localhost:2026
```

## Run the demo

```bash
./examples/tutorials/acp-coding-agents/acp_tutorial.sh
```

The script will:

1. List all available ACP agents
2. Show agent configuration (system prompt, enabled skills)
3. Set a custom system prompt for an agent
4. Call multiple agents in parallel with the same prompt
5. Resume a session (multi-turn conversation)
6. List running processes
7. View call history
8. Clean up

## CLI reference

```bash
# List agents
nexus acp agents

# Call an agent
nexus acp call -a claude -p "Explain this function"
nexus acp call -a gemini -p "Fix the bug" --cwd /path/to/project --timeout 600

# Resume a session (multi-turn)
nexus acp call -a claude -p "Follow up question" -s <session_id>

# System prompts
nexus acp system-prompt get -a claude
nexus acp system-prompt set -a claude -c "You are a concise coding assistant."

# Agent config (view/update skills and system prompt)
nexus acp config -a claude
nexus acp config -a claude --skills /path/to/skill1.md,/path/to/skill2.md
nexus acp config -a claude --system-prompt "Be brief."

# Process management
nexus acp ps
nexus acp kill <pid>

# History
nexus acp history
nexus acp history -n 10
```

## How it works

When you run `nexus acp call`, the CLI sends a JSON-RPC request over gRPC to
the `acp_rpc` service running inside nexusd. The service:

1. Looks up the agent configuration (command, args, env vars)
2. Spawns the agent binary as a subprocess with the prompt on stdin
3. Captures stdout/stderr and parses metadata (model, tokens, cost, session ID)
4. Returns the result to the CLI

All agents use the same interface — the ACP adapter translates between the
unified protocol and each agent's native CLI format.

## Supported agents

| Agent ID   | Binary     | Description      |
|------------|------------|------------------|
| `claude`   | `claude`   | Claude Code      |
| `codex`    | `codex`    | Codex CLI        |
| `gemini`   | `gemini`   | Gemini CLI       |
| `qwen`     | `qwen`     | Qwen Code        |
| `goose`    | `goose`    | Goose            |
| `copilot`  | `copilot`  | GitHub Copilot   |
| `auggie`   | `auggie`   | Augment Code     |
| `opencode` | `opencode` | OpenCode         |
| `droid`    | `droid`    | Factory Droid    |
| `kimi`     | `kimi`     | Kimi CLI         |

Run `nexus acp agents` to see the full list with enabled status.
