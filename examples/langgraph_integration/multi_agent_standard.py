"""
Multi-Agent LangGraph Example with Standard File I/O
======================================================

This example demonstrates a multi-agent workflow with:
- Researcher: Investigates requirements and writes specs
- Coder: Reads specs and implements code
- Reviewer: Reviews code and provides feedback

Uses standard Python file I/O (os, open, write).
"""

import os
from typing import Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph


# State definition for the multi-agent workflow
class AgentState(TypedDict):
    task: str
    current_agent: str
    research_file: str
    code_file: str
    review_file: str
    iteration: int
    max_iterations: int


def researcher_node(state: AgentState) -> AgentState:
    """Researcher agent: analyzes task and writes requirements."""
    print(f"\n🔍 Researcher is analyzing task: {state['task']}")

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

    # Write requirements to file using standard file I/O
    os.makedirs("workspace/research", exist_ok=True)
    research_file = "workspace/research/requirements.txt"

    with open(research_file, "w") as f:
        f.write(requirements)

    print(f"✓ Requirements written to {research_file}")

    return {**state, "research_file": research_file, "current_agent": "coder"}


def coder_node(state: AgentState) -> AgentState:
    """Coder agent: reads requirements and writes code."""
    print("\n💻 Coder is implementing solution...")

    # Read requirements from file
    with open(state["research_file"]) as f:
        requirements = f.read()

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

    # Write code to file
    os.makedirs("workspace/code", exist_ok=True)
    code_file = "workspace/code/implementation.py"

    with open(code_file, "w") as f:
        f.write(code)

    print(f"✓ Code written to {code_file}")

    return {**state, "code_file": code_file, "current_agent": "reviewer"}


def reviewer_node(state: AgentState) -> AgentState:
    """Reviewer agent: reviews code and provides feedback."""
    print("\n📋 Reviewer is evaluating code...")

    # Read code from file
    with open(state["code_file"]) as f:
        code = f.read()

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.5)

    messages = [
        SystemMessage(
            content="You are a code reviewer. Provide constructive feedback on code quality, best practices, and potential improvements."
        ),
        HumanMessage(content=f"Code to review:\n{code}\n\nProvide detailed review feedback."),
    ]

    response = llm.invoke(messages)
    review = response.content

    # Write review to file
    os.makedirs("workspace/reviews", exist_ok=True)
    review_file = "workspace/reviews/review.txt"

    with open(review_file, "w") as f:
        f.write(review)

    print(f"✓ Review written to {review_file}")

    return {
        **state,
        "review_file": review_file,
        "current_agent": "done",
        "iteration": state["iteration"] + 1,
    }


def route_agent(state: AgentState) -> Literal["researcher", "coder", "reviewer", END]:
    """Router function to determine next agent."""
    current = state["current_agent"]

    routing_map = {
        "start": "researcher",
        "researcher": "coder",
        "coder": "reviewer",
        "done": END,
    }
    return routing_map.get(current, END)


def build_graph():
    """Build the multi-agent workflow graph."""
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


def main():
    """Main function to run the multi-agent workflow."""
    print("=" * 60)
    print("Multi-Agent Workflow: Standard File I/O")
    print("=" * 60)

    # Clean up previous workspace
    import shutil

    if os.path.exists("workspace"):
        shutil.rmtree("workspace")

    # Initialize state
    initial_state: AgentState = {
        "task": "Create a simple calculator class that can add, subtract, multiply, and divide two numbers",
        "current_agent": "start",
        "research_file": "",
        "code_file": "",
        "review_file": "",
        "iteration": 0,
        "max_iterations": 1,
    }

    # Build and run the graph
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
    print("\nNote: All agents have full read/write access to all files.")
    print("No permission control in this standard implementation.")


if __name__ == "__main__":
    main()
