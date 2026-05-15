"""ACP CLI commands — call coding agents via ACP JSON-RPC.

Provides ``nexus acp`` command group for interacting with ACP agents
(Claude Code, Codex, Gemini CLI, etc.) through the nexusd RPC layer.
"""

from __future__ import annotations

import os
import re
from typing import Any

import click
from rich.table import Table

from nexus.cli.theme import console
from nexus.cli.utils import add_backend_options, get_filesystem, handle_error


def _parse_readme_md(path: str) -> dict:
    """Read a skill .md file and extract name, description from YAML frontmatter."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise click.BadParameter(f"Skill file not found: {path}")

    with open(path) as f:
        content = f.read()

    # Parse YAML frontmatter (between --- delimiters)
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if fm_match is None:
        raise click.BadParameter(f"No YAML frontmatter found in {path}")

    fm_text = fm_match.group(1)
    name = None
    description = None
    for line in fm_text.splitlines():
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("description:"):
            description = line.split(":", 1)[1].strip()

    if not name:
        raise click.BadParameter(f"Missing 'name' in frontmatter of {path}")

    return {
        "name": name,
        "description": description or "",
        "path": os.path.abspath(path),
    }


@click.group(name="acp")
def acp() -> None:
    """Coding agent operations via ACP (Agent Communication Protocol).

    Call, list, and manage coding agents (Claude Code, Codex, Gemini CLI, etc.)
    through the ACP JSON-RPC protocol.

    Examples:
        nexus acp agents
        nexus acp call -a claude -p "What is 2+2?"
        nexus acp config -a claude --skills /path/to/pdf.md,/path/to/xlsx.md
        nexus acp ps
        nexus acp kill <pid>
    """


@acp.command(name="call")
@click.option("-a", "--agent", "agent_id", required=True, type=str, help="Agent ID")
@click.option("-p", "--prompt", required=True, type=str, help="Prompt to send")
@click.option("--cwd", default=".", help="Working directory for the agent")
@click.option("--timeout", default=300.0, type=float, help="Timeout in seconds")
@click.option(
    "-s", "--session", "session_id", default=None, type=str, help="Resume a previous session by ID"
)
@add_backend_options
def call_agent(
    agent_id: str,
    prompt: str,
    cwd: str,
    timeout: float,
    session_id: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Call a coding agent with a prompt.

    Examples:
        nexus acp call -a claude -p "What is 2+2?"
        nexus acp call -a codex -p "Refactor this function" --cwd /path/to/project
        nexus acp call -a claude -p "Fix the bug" --timeout 600
        nexus acp call -a claude -p "Follow up" -s <session_id>
    """
    import asyncio

    asyncio.run(
        _async_call_agent(agent_id, prompt, cwd, timeout, session_id, remote_url, remote_api_key)
    )


async def _async_call_agent(
    agent_id: str,
    prompt: str,
    cwd: str,
    timeout: float,
    session_id: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)
        svc = nx.service("acp_rpc")
        if svc is None:
            console.print("[nexus.error]ACP service not available[/nexus.error]")
            nx.close()
            return

        result = svc.acp_call(
            agent_id=agent_id,
            prompt=prompt,
            cwd=cwd,
            timeout=timeout,
            session_id=session_id,
        )

        # Display result
        if result.get("exit_code", -1) == 0:
            console.print(result.get("response", ""))
            meta = result.get("metadata", {})
            if meta:
                parts = []
                if meta.get("session_id"):
                    parts.append(f"session={meta['session_id']}")
                if meta.get("model"):
                    parts.append(f"model={meta['model']}")
                if meta.get("input_tokens"):
                    parts.append(f"in={meta['input_tokens']}")
                if meta.get("output_tokens"):
                    parts.append(f"out={meta['output_tokens']}")
                if meta.get("cost_usd"):
                    parts.append(f"cost=${meta['cost_usd']:.4f}")
                if parts:
                    console.print(f"\n[nexus.muted]({', '.join(parts)})[/nexus.muted]")
        else:
            console.print(
                f"[nexus.error]Agent failed (exit_code={result.get('exit_code')})[/nexus.error]"
            )
            if result.get("stderr"):
                console.print(f"[nexus.muted]{result['stderr']}[/nexus.muted]")
            if result.get("timed_out"):
                console.print("[nexus.warning]Agent timed out[/nexus.warning]")

        nx.close()
    except Exception as e:
        handle_error(e)


@acp.command(name="agents")
@add_backend_options
def list_agents(
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List available ACP agent configurations.

    Examples:
        nexus acp agents
    """
    import asyncio

    asyncio.run(_async_list_agents(remote_url, remote_api_key))


async def _async_list_agents(
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)
        svc = nx.service("acp_rpc")
        if svc is None:
            console.print("[nexus.error]ACP service not available[/nexus.error]")
            nx.close()
            return

        agents = svc.acp_list_agents()

        if not agents:
            console.print("[nexus.warning]No agents configured[/nexus.warning]")
            nx.close()
            return

        table = Table(title="ACP Agents")
        table.add_column("Agent ID", style="nexus.value")
        table.add_column("Name", style="nexus.success")
        table.add_column("Command", style="nexus.muted")
        table.add_column("Enabled", style="nexus.muted")

        for agent in agents:
            table.add_row(
                agent["agent_id"],
                agent.get("name", ""),
                agent.get("command", ""),
                "yes" if agent.get("enabled") else "no",
            )

        console.print(table)
        nx.close()
    except Exception as e:
        handle_error(e)


@acp.command(name="ps")
@add_backend_options
def list_processes(
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """List running ACP agent processes.

    Examples:
        nexus acp ps
    """
    import asyncio

    asyncio.run(_async_list_processes(remote_url, remote_api_key))


async def _async_list_processes(
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)
        svc = nx.service("acp_rpc")
        if svc is None:
            console.print("[nexus.error]ACP service not available[/nexus.error]")
            nx.close()
            return

        procs = svc.acp_list_processes()

        if not procs:
            console.print("[nexus.warning]No ACP processes[/nexus.warning]")
            nx.close()
            return

        table = Table(title="ACP Processes")
        table.add_column("PID", style="nexus.value")
        table.add_column("Name", style="nexus.success")
        table.add_column("State", style="nexus.warning")
        table.add_column("Owner", style="nexus.muted")

        for p in procs:
            table.add_row(
                p["pid"],
                p.get("name", ""),
                p.get("state", ""),
                p.get("owner_id", ""),
            )

        console.print(table)
        nx.close()
    except Exception as e:
        handle_error(e)


@acp.command(name="history")
@click.option("-n", "--limit", default=20, type=int, help="Max number of entries")
@add_backend_options
def history(
    limit: int,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Show ACP agent call history.

    Examples:
        nexus acp history
        nexus acp history -n 5
    """
    import asyncio

    asyncio.run(_async_history(limit, remote_url, remote_api_key))


async def _async_history(
    limit: int,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)
        svc = nx.service("acp_rpc")
        if svc is None:
            console.print("[nexus.error]ACP service not available[/nexus.error]")
            nx.close()
            return

        entries = svc.acp_history(limit=limit)

        if not entries:
            console.print("[nexus.warning]No call history[/nexus.warning]")
            nx.close()
            return

        table = Table(title="ACP Call History")
        table.add_column("PID", style="nexus.value", no_wrap=True)
        table.add_column("Agent", style="nexus.success")
        table.add_column("Session", style="nexus.muted", no_wrap=True)
        table.add_column("Exit", style="nexus.muted")
        table.add_column("Prompt", style="nexus.warning", max_width=40)
        table.add_column("Response", max_width=40)
        table.add_column("Model", style="nexus.muted")
        table.add_column("Tokens", style="nexus.muted")
        table.add_column("Cost", style="nexus.muted")

        for entry in entries:
            meta = entry.get("metadata", {})
            prompt = entry.get("prompt", "")
            response = entry.get("response", "")
            if len(prompt) > 40:
                prompt = prompt[:37] + "..."
            if len(response) > 40:
                response = response[:37] + "..."
            cost = f"${meta['cost_usd']:.4f}" if meta.get("cost_usd") else ""
            exit_code = str(entry.get("exit_code", ""))
            if entry.get("timed_out"):
                exit_code = "timeout"
            # Token summary: in/out
            tokens = ""
            in_t = meta.get("input_tokens")
            out_t = meta.get("output_tokens")
            if in_t or out_t:
                tokens = f"{in_t or 0}/{out_t or 0}"
            # Session ID — show first 8 chars
            sid = entry.get("session_id") or meta.get("session_id") or ""
            if sid:
                sid = sid[:8]
            table.add_row(
                entry.get("pid", "")[:12],
                entry.get("agent_id", ""),
                sid,
                exit_code,
                prompt,
                response,
                meta.get("model", ""),
                tokens,
                cost,
            )

        console.print(table)
        nx.close()
    except Exception as e:
        handle_error(e)


@acp.command(name="kill")
@click.argument("pid", type=str)
@add_backend_options
def kill_process(
    pid: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Kill a running ACP agent process.

    Examples:
        nexus acp kill <pid>
    """
    import asyncio

    asyncio.run(_async_kill_process(pid, remote_url, remote_api_key))


async def _async_kill_process(
    pid: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)
        svc = nx.service("acp_rpc")
        if svc is None:
            console.print("[nexus.error]ACP service not available[/nexus.error]")
            nx.close()
            return

        result = svc.acp_kill(pid=pid)
        console.print(
            f"[nexus.success]Killed[/nexus.success] {result.get('name', pid)} "
            f"(state={result.get('state', 'unknown')})"
        )

        nx.close()
    except Exception as e:
        handle_error(e)


@acp.command(name="config")
@click.option("-a", "--agent", "agent_id", required=True, type=str, help="Agent ID")
@click.option(
    "--skills",
    default=None,
    type=str,
    help="Comma-separated paths to skill .md files (empty string to clear)",
)
@click.option("--system-prompt", default=None, type=str, help="Set the system prompt")
@add_backend_options
def config_agent(
    agent_id: str,
    skills: str | None,
    system_prompt: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """View or update persistent agent configuration.

    With no options, shows current config. Use --skills and/or --system-prompt to set values.

    Examples:
        nexus acp config -a claude
        nexus acp config -a claude --skills /path/to/pdf.md,/path/to/xlsx.md
        nexus acp config -a claude --skills ""
        nexus acp config -a claude --system-prompt "You are helpful"
    """
    import asyncio

    asyncio.run(_async_config_agent(agent_id, skills, system_prompt, remote_url, remote_api_key))


async def _async_config_agent(
    agent_id: str,
    skills: str | None,
    system_prompt: str | None,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)
        svc = nx.service("acp_rpc")
        if svc is None:
            console.print("[nexus.error]ACP service not available[/nexus.error]")
            nx.close()
            return

        made_changes = False

        # Set skills if provided
        if skills is not None:
            paths = [s.strip() for s in skills.split(",") if s.strip()]
            if paths:
                skill_list = [_parse_readme_md(p) for p in paths]
                svc.acp_set_enabled_skills(agent_id=agent_id, skills=skill_list)
                names = [sk["name"] for sk in skill_list]
                console.print(
                    f"[nexus.success]Set enabled skills for {agent_id}:[/nexus.success] {', '.join(names)}"
                )
            else:
                svc.acp_set_enabled_skills(agent_id=agent_id, skills=[])
                console.print(
                    f"[nexus.success]Cleared enabled skills for {agent_id}[/nexus.success]"
                )
            made_changes = True

        # Set system prompt if provided
        if system_prompt is not None:
            svc.acp_set_system_prompt(agent_id=agent_id, content=system_prompt)
            console.print(
                f"[nexus.success]Set system prompt for {agent_id}[/nexus.success] ({len(system_prompt)} chars)"
            )
            made_changes = True

        # If no setters, show current config
        if not made_changes:
            skills_result = svc.acp_get_enabled_skills(agent_id=agent_id)
            prompt_result = svc.acp_get_system_prompt(agent_id=agent_id)

            console.print(f"[bold]Config for {agent_id}[/bold]\n")

            current_skills = skills_result.get("skills")
            if current_skills:
                table = Table(show_header=True)
                table.add_column("Name", style="nexus.value")
                table.add_column("Description", style="nexus.muted")
                table.add_column("Path", style="nexus.muted")
                for sk in current_skills:
                    table.add_row(sk["name"], sk.get("description", ""), sk.get("path", ""))
                console.print("[nexus.value]Enabled skills:[/nexus.value]")
                console.print(table)
            else:
                console.print("[nexus.muted]Enabled skills: (none)[/nexus.muted]")

            current_prompt = prompt_result.get("content")
            if current_prompt:
                console.print(f"[nexus.value]System prompt:[/nexus.value] {current_prompt}")
            else:
                console.print("[nexus.muted]System prompt: (none)[/nexus.muted]")

        nx.close()
    except Exception as e:
        handle_error(e)


@acp.group(name="system-prompt")
def system_prompt() -> None:
    """Manage ACP agent system prompts."""


@system_prompt.command(name="get")
@click.option("-a", "--agent", "agent_id", required=True, type=str, help="Agent ID")
@add_backend_options
def get_system_prompt(
    agent_id: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """View the system prompt for an ACP agent.

    Examples:
        nexus acp system-prompt get -a claude
    """
    import asyncio

    asyncio.run(_async_get_system_prompt(agent_id, remote_url, remote_api_key))


async def _async_get_system_prompt(
    agent_id: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)
        svc = nx.service("acp_rpc")
        if svc is None:
            console.print("[nexus.error]ACP service not available[/nexus.error]")
            nx.close()
            return

        result = svc.acp_get_system_prompt(agent_id=agent_id)
        content = result.get("content")
        if content:
            console.print(content)
        else:
            console.print(f"[nexus.warning]No system prompt set for {agent_id}[/nexus.warning]")

        nx.close()
    except Exception as e:
        handle_error(e)


@system_prompt.command(name="set")
@click.option("-a", "--agent", "agent_id", required=True, type=str, help="Agent ID")
@click.option("-c", "--content", required=True, type=str, help="System prompt content")
@add_backend_options
def set_system_prompt(
    agent_id: str,
    content: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    """Set the system prompt for an ACP agent.

    Examples:
        nexus acp system-prompt set -a claude -c "You are a helpful coding assistant."
    """
    import asyncio

    asyncio.run(_async_set_system_prompt(agent_id, content, remote_url, remote_api_key))


async def _async_set_system_prompt(
    agent_id: str,
    content: str,
    remote_url: str | None,
    remote_api_key: str | None,
) -> None:
    try:
        nx: Any = await get_filesystem(remote_url, remote_api_key)
        svc = nx.service("acp_rpc")
        if svc is None:
            console.print("[nexus.error]ACP service not available[/nexus.error]")
            nx.close()
            return

        result = svc.acp_set_system_prompt(agent_id=agent_id, content=content)
        console.print(
            f"[nexus.success]Set system prompt for {agent_id}[/nexus.success] ({result.get('length', 0)} chars)"
        )

        nx.close()
    except Exception as e:
        handle_error(e)
