"""A real LangGraph application with two nodes; model capability supplied by host."""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class State(TypedDict):
    query: str
    answer: str


def build_graph(model, trace):
    def normalize(state):
        trace("normalize", "started")
        result = {"query": state["query"].strip()}
        trace("normalize", "completed")
        return result

    def generate(state):
        trace("generate", "started")
        answer = model(
            [
                {
                    "role": "system",
                    "content": "Calculate the arithmetic expression. Output the integer result only.",
                },
                {"role": "user", "content": state["query"]},
            ],
            max_tokens=128,
            temperature=0,
        )
        trace("generate", "completed")
        return {"answer": answer}

    graph = StateGraph(State)
    graph.add_node("normalize", normalize)
    graph.add_node("generate", generate)
    graph.add_edge(START, "normalize")
    graph.add_edge("normalize", "generate")
    graph.add_edge("generate", END)
    return graph.compile()
