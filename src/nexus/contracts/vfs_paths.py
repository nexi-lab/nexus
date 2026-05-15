"""VFS path conventions — single source of truth for all VFS path patterns.

Zero nexus.* imports. Used by services (AgentStatusResolver,
TaskManagerService), the Rust-side AcpService (rust/kernel/src/acp/paths.rs
mirrors these conventions), and bricks alike.

Every VFS path pattern in the system should be constructed through this
module. No inline f-string path construction elsewhere.

Categories:
    proc:   /{zone}/proc/{pid}/...          — process (agent) runtime
    agent:  /{zone}/agents/{id}/...         — agent configuration
    llm:    /{zone}/llm/{provider}/...      — LLM backend mount
    task:   /.tasks/...                     — task management

References:
    - contracts/constants.py — SYSTEM_PATH_PREFIX
    - services/agents/agent_status_resolver.py — AgentStatusResolver trie patterns
    - rust/kernel/src/acp/paths.rs — Rust-side path mirror
"""


class proc:
    """Process (agent) runtime paths: /{zone}/proc/{pid}/..."""

    @staticmethod
    def fd(zone_id: str, pid: str, fd_num: int) -> str:
        """DT_PIPE file descriptor: /{zone}/proc/{pid}/fd/{0,1,2}"""
        return f"/{zone_id}/proc/{pid}/fd/{fd_num}"

    @staticmethod
    def result(zone_id: str, pid: str) -> str:
        """Agent turn result (JSON): /{zone}/proc/{pid}/result"""
        return f"/{zone_id}/proc/{pid}/result"

    @staticmethod
    def status(zone_id: str, pid: str) -> str:
        """Virtual procfs status (read-only): /{zone}/proc/{pid}/status"""
        return f"/{zone_id}/proc/{pid}/status"

    @staticmethod
    def root(zone_id: str, pid: str) -> str:
        """Process root directory: /{zone}/proc/{pid}"""
        return f"/{zone_id}/proc/{pid}"


class agent:
    """Agent configuration paths: /{zone}/agents/{id}/..."""

    @staticmethod
    def root(zone_id: str, agent_id: str) -> str:
        """Agent config directory: /{zone}/agents/{id}"""
        return f"/{zone_id}/agents/{agent_id}"

    @staticmethod
    def config(zone_id: str, agent_id: str) -> str:
        """Agent config file: /{zone}/agents/{id}/agent.json"""
        return f"/{zone_id}/agents/{agent_id}/agent.json"

    @staticmethod
    def system_prompt(zone_id: str, agent_id: str) -> str:
        """System prompt override: /{zone}/agents/{id}/SYSTEM.md"""
        return f"/{zone_id}/agents/{agent_id}/SYSTEM.md"

    @staticmethod
    def tools(zone_id: str, agent_id: str) -> str:
        """Tool definitions: /{zone}/agents/{id}/tools.json"""
        return f"/{zone_id}/agents/{agent_id}/tools.json"

    @staticmethod
    def skills(zone_id: str, agent_id: str) -> str:
        """Enabled skills config: /{zone}/agents/{id}/config"""
        return f"/{zone_id}/agents/{agent_id}/config"

    @staticmethod
    def conversation(zone_id: str, agent_id: str) -> str:
        """Conversation state (CAS-addressed): /{zone}/agents/{id}/conversation"""
        return f"/{zone_id}/agents/{agent_id}/conversation"

    @staticmethod
    def sessions_dir(zone_id: str, agent_id: str) -> str:
        """Sessions directory: /{zone}/agents/{id}/sessions"""
        return f"/{zone_id}/agents/{agent_id}/sessions"

    @staticmethod
    def prompt_fragment(zone_id: str, agent_id: str, name: str) -> str:
        """Prompt fragment file: /{zone}/agents/{id}/prompts/{name}.md"""
        return f"/{zone_id}/agents/{agent_id}/prompts/{name}.md"

    @staticmethod
    def transcript(zone_id: str, agent_id: str, timestamp: str) -> str:
        """Compaction transcript: /{zone}/agents/{id}/transcripts/{timestamp}.jsonl"""
        return f"/{zone_id}/agents/{agent_id}/transcripts/{timestamp}.jsonl"

    @staticmethod
    def session_conversation(zone_id: str, agent_id: str, session_id: str) -> str:
        """Session conversation (CAS-addressed): /{zone}/agents/{id}/sessions/{session}/conversation"""
        return f"/{zone_id}/agents/{agent_id}/sessions/{session_id}/conversation"

    @staticmethod
    def session_metadata(zone_id: str, agent_id: str, session_id: str) -> str:
        """Session metadata: /{zone}/agents/{id}/sessions/{session}/metadata.json"""
        return f"/{zone_id}/agents/{agent_id}/sessions/{session_id}/metadata.json"


class llm:
    """LLM backend paths: /{zone}/llm/{provider}/..."""

    @staticmethod
    def stream(llm_mount: str, stream_id: str) -> str:
        """DT_STREAM for LLM token delivery: {llm_mount}/.streams/{stream_id}"""
        return f"{llm_mount}/.streams/{stream_id}"


class task:
    """Task management paths: /.tasks/..."""

    ROOT = "/.tasks"

    @staticmethod
    def mission(mission_id: str) -> str:
        return f"/.tasks/missions/{mission_id}.json"

    @staticmethod
    def item(task_id: str) -> str:
        return f"/.tasks/tasks/{task_id}.json"

    @staticmethod
    def artifact(artifact_id: str) -> str:
        return f"/.tasks/artifacts/{artifact_id}.json"

    @staticmethod
    def comment(task_id: str, comment_id: str) -> str:
        return f"/.tasks/comments/{task_id}/{comment_id}.json"

    @staticmethod
    def audit_entry(task_id: str, entry_id: str) -> str:
        return f"/.tasks/audit/{task_id}/{entry_id}.json"

    @staticmethod
    def agent_status(task_id: str) -> str:
        """Virtual: live ProcessDescriptor for task's agent."""
        return f"/.tasks/tasks/{task_id}/agent/status"
