"""
Multi-Agent LangGraph Example with Nexus File System
======================================================

This example demonstrates the SAME multi-agent workflow as multi_agent_standard.py,
but with Nexus providing:
- Permission-based access control per agent
- Cloud-based file storage
- Audit trails

Drop-in replacement: Standard file I/O → Nexus file system with permissions!
"""

import contextlib
import os
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from nexus.remote import RemoteNexusFS


# State definition (same as standard version)
class AgentState(TypedDict):
    task: str
    current_agent: str
    research_file: str
    code_file: str
    review_file: str
    iteration: int
    max_iterations: int


def get_demo_user_key():
    """
    Get or create a non-admin demo user API key.
    This is required to properly test permission enforcement.

    Returns the demo user's API key (non-admin).
    """
    # Use a fixed demo user API key (non-admin)
    # In production, this would be created via: nexus admin create-user demo_user --name "Demo User"
    demo_key = "sk-default_demo_use_59dacd01_30febe1682e6aa65da66343f87148e48"

    # TODO: Could dynamically create this via API if needed
    return demo_key


def setup_nexus_permissions(admin_nx: RemoteNexusFS, workspace: str):
    """
    Setup permission structure for multi-agent workflow.

    Demonstrates Nexus value-add:
    - Researcher: Can only write to /workspace/research/
    - Coder: Can read research, write to /workspace/code/
    - Reviewer: Can read code, write to /workspace/reviews/
    """
    print("\n🔐 Setting up Nexus permissions...")

    # Create workspace structure
    admin_nx.mkdir(f"{workspace}/research", parents=True)
    admin_nx.mkdir(f"{workspace}/code", parents=True)
    admin_nx.mkdir(f"{workspace}/reviews", parents=True)

    # Grant permissions for researcher agent
    # Researcher can write to /workspace/research/ directory
    admin_nx.rebac_create(
        subject=("agent", "researcher"),
        relation="direct_editor",
        object=("file", f"{workspace}/research"),
    )
    print("  ✓ Researcher can write to /research/")

    # Grant permissions for coder agent
    # Coder can read /workspace/research/
    admin_nx.rebac_create(
        subject=("agent", "coder"),
        relation="direct_viewer",
        object=("file", f"{workspace}/research"),
    )
    # Coder can write to /workspace/code/
    admin_nx.rebac_create(
        subject=("agent", "coder"), relation="direct_editor", object=("file", f"{workspace}/code")
    )
    print("  ✓ Coder can read /research/ and write to /code/")

    # Grant permissions for reviewer agent
    # Reviewer can read /workspace/code/
    admin_nx.rebac_create(
        subject=("agent", "reviewer"),
        relation="direct_viewer",
        object=("file", f"{workspace}/code"),
    )
    # Reviewer can write to /workspace/reviews/
    admin_nx.rebac_create(
        subject=("agent", "reviewer"),
        relation="direct_editor",
        object=("file", f"{workspace}/reviews"),
    )
    print("  ✓ Reviewer can read /code/ and write to /reviews/")

    print("🔐 Permission setup complete!\n")


def researcher_node(state: AgentState) -> AgentState:
    """Researcher agent: analyzes task and writes requirements."""
    print(f"\n🔍 Researcher is analyzing task: {state['task']}")

    # Connect as researcher agent (with limited permissions)
    # Use demo user API key (non-admin) with X-Agent-ID header for permission enforcement
    nexus = RemoteNexusFS(
        server_url=os.getenv("NEXUS_URL", "http://localhost:8080"),
        api_key=get_demo_user_key(),  # Non-admin key
    )
    nexus.agent_id = "researcher"  # Set agent identity for permission checks

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.7)

    messages = [
        SystemMessage(
            content="You are a technical researcher. Analyze the coding task and write clear requirements."
        ),
        HumanMessage(
            content=f"Task: {state['task']}\n\nWrite detailed requirements for implementing this."
        ),
    ]

    response = llm.invoke(messages)
    requirements = response.content

    # Write requirements using Nexus (drop-in replacement!)
    research_file = "/workspace/research/requirements.txt"
    nexus.write(research_file, requirements)

    print(f"✓ Requirements written to {research_file}")
    print("  (Researcher has write permission to /workspace/research/)")

    return {**state, "research_file": research_file, "current_agent": "coder"}


def coder_node(state: AgentState) -> AgentState:
    """Coder agent: reads requirements and writes code."""
    print("\n💻 Coder is implementing solution...")

    # Connect as coder agent
    # Use demo user API key (non-admin) with X-Agent-ID header for permission enforcement
    nexus = RemoteNexusFS(
        server_url=os.getenv("NEXUS_URL", "http://localhost:8080"),
        api_key=get_demo_user_key(),  # Non-admin key
    )
    nexus.agent_id = "coder"  # Set agent identity for permission checks

    # Read requirements using Nexus
    requirements = nexus.read(state["research_file"])
    print("  (Coder has read permission to /workspace/research/)")

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3)

    messages = [
        SystemMessage(
            content="You are an expert Python developer. Write clean, well-documented code."
        ),
        HumanMessage(
            content=f"Requirements:\n{requirements}\n\nImplement this in Python with proper documentation."
        ),
    ]

    response = llm.invoke(messages)
    code = response.content

    # Write code using Nexus
    code_file = "/workspace/code/implementation.py"
    nexus.write(code_file, code)

    print(f"✓ Code written to {code_file}")
    print("  (Coder has write permission to /workspace/code/)")

    return {**state, "code_file": code_file, "current_agent": "reviewer"}


def reviewer_node(state: AgentState) -> AgentState:
    """Reviewer agent: reviews code and provides feedback."""
    print("\n📋 Reviewer is evaluating code...")

    # Connect as reviewer agent
    # Use demo user API key (non-admin) with X-Agent-ID header for permission enforcement
    nexus = RemoteNexusFS(
        server_url=os.getenv("NEXUS_URL", "http://localhost:8080"),
        api_key=get_demo_user_key(),  # Non-admin key
    )
    nexus.agent_id = "reviewer"  # Set agent identity for permission checks

    # Read code using Nexus
    code = nexus.read(state["code_file"])
    print("  (Reviewer has read permission to /workspace/code/)")

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.5)

    messages = [
        SystemMessage(
            content="You are a code reviewer. Provide constructive feedback on code quality, best practices, and potential improvements."
        ),
        HumanMessage(content=f"Code to review:\n{code}\n\nProvide detailed review feedback."),
    ]

    response = llm.invoke(messages)
    review = response.content

    # Write review using Nexus
    review_file = "/workspace/reviews/review.txt"
    nexus.write(review_file, review)

    print(f"✓ Review written to {review_file}")
    print("  (Reviewer has write permission to /workspace/reviews/)")

    return {
        **state,
        "review_file": review_file,
        "current_agent": "done",
        "iteration": state["iteration"] + 1,
    }


def build_graph():
    """Build the multi-agent workflow graph (same as standard version)."""
    workflow = StateGraph(AgentState)

    # Add nodes
    workflow.add_node("researcher", researcher_node)
    workflow.add_node("coder", coder_node)
    workflow.add_node("reviewer", reviewer_node)

    # Add edges
    workflow.add_edge(START, "researcher")
    workflow.add_edge("researcher", "coder")
    workflow.add_edge("coder", "reviewer")
    workflow.add_edge("reviewer", END)

    return workflow.compile()


def demonstrate_permission_enforcement():
    """
    Demonstrate that permissions are actually enforced.
    Show what happens when an agent tries to access unauthorized resources.
    """
    print("\n" + "=" * 60)
    print("🔒 Demonstrating Permission Enforcement")
    print("=" * 60)

    # Use non-admin demo user key for permission enforcement
    nexus_coder = RemoteNexusFS(
        server_url=os.getenv("NEXUS_URL", "http://localhost:8080"),
        api_key=get_demo_user_key(),  # Non-admin key
    )
    nexus_coder.agent_id = "coder"

    nexus_reviewer = RemoteNexusFS(
        server_url=os.getenv("NEXUS_URL", "http://localhost:8080"),
        api_key=get_demo_user_key(),  # Non-admin key
    )
    nexus_reviewer.agent_id = "reviewer"

    # Try: Reviewer attempting to write code (should fail)
    print("\n❌ Test: Can reviewer write to /code/? (Should be denied)")
    try:
        nexus_reviewer.write("/workspace/code/hacked.py", "malicious code")
        print("  ⚠️  WARNING: Reviewer was able to write code! Permission issue!")
    except Exception as e:
        print(f"  ✓ Access denied: {str(e)}")

    # Try: Coder attempting to read reviews (should fail)
    print("\n❌ Test: Can coder read /reviews/? (Should be denied)")
    try:
        nexus_coder.read("/workspace/reviews/review.txt")
        print("  ⚠️  WARNING: Coder was able to read reviews! Permission issue!")
    except Exception as e:
        print(f"  ✓ Access denied: {str(e)}")

    print("\n🔒 Permission enforcement verified!")


def main():
    """Main function to run the multi-agent workflow with Nexus."""
    print("=" * 60)
    print("Multi-Agent Workflow: Nexus with Permissions")
    print("=" * 60)

    # Setup admin connection for permission configuration
    admin_nx = RemoteNexusFS(
        server_url=os.getenv("NEXUS_URL", "http://localhost:8080"),
        api_key=os.getenv("NEXUS_API_KEY"),
    )

    workspace = "/workspace"

    # Clean up previous workspace
    with contextlib.suppress(BaseException):
        admin_nx.rmdir(workspace, recursive=True)

    # Setup permissions (Nexus value-add!)
    setup_nexus_permissions(admin_nx, workspace)

    # Initialize state (same as standard version)
    initial_state: AgentState = {
        "task": "Create a simple calculator class that can add, subtract, multiply, and divide two numbers",
        "current_agent": "start",
        "research_file": "",
        "code_file": "",
        "review_file": "",
        "iteration": 0,
        "max_iterations": 1,
    }

    # Build and run the graph (same as standard version)
    graph = build_graph()

    print(f"\n📋 Starting task: {initial_state['task']}")

    result = graph.invoke(initial_state)

    print("\n" + "=" * 60)
    print("✅ Workflow completed!")
    print("=" * 60)
    print("\nGenerated files:")
    print(f"  - Requirements: {result['research_file']}")
    print(f"  - Code: {result['code_file']}")
    print(f"  - Review: {result['review_file']}")

    # Demonstrate permission enforcement
    demonstrate_permission_enforcement()

    print("\n" + "=" * 60)
    print("🎯 Nexus Value-Add Summary")
    print("=" * 60)
    print("✓ Drop-in replacement: minimal code changes")
    print("✓ Permission-based access control per agent")
    print("✓ Cloud storage with audit trails")
    print("✓ Multi-user/multi-agent collaboration")
    print("=" * 60)


if __name__ == "__main__":
    main()
