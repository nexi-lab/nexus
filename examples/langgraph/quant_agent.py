#!/usr/bin/env python3
"""Quantitative Trading Strategy Agent using LangGraph's Prebuilt create_react_agent.

This agent specializes in writing and analyzing quantitative trading strategies,
with access to Nexus filesystem containing trading API documentation and data.

Authentication:
    API keys are REQUIRED via metadata.x_auth: "Bearer <token>"
    Frontend automatically passes the authenticated user's API key in request metadata.
    Each tool extracts and uses the token to create an authenticated RemoteNexusFS instance.

Requirements:
    pip install langgraph langchain-anthropic

Usage from Frontend (HTTP):
    POST http://localhost:2024/runs/stream
    {
        "assistant_id": "quant_agent",
        "input": {
            "messages": [{"role": "user", "content": "Write a mean reversion strategy"}]
        },
        "metadata": {
            "x_auth": "Bearer sk-your-api-key-here",
            "user_id": "user-123",
            "tenant_id": "tenant-123",
            "opened_file_path": "/workspace/admin/strategy.py"  // Optional: currently opened file
        }
    }

    Note: The frontend automatically includes x_auth and opened_file_path in metadata when user is logged in.
"""

import os

import requests
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from nexus_tools import get_nexus_tools

# Import official system prompt from Nexus tools
from nexus.tools import CODING_AGENT_SYSTEM_PROMPT


# Backtest tool for qlib strategies
@tool
def run_backtest(
    strategy_code: str,
    start_date: str,
    end_date: str,
    stocks: list[str],
    init_cash: int = 100000,
    frequency: str = "daily",
) -> str:
    """Run backtest for a qlib trading strategy.

    Args:
        strategy_code: Python code with UserStrategy class and generate_signals method
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        stocks: List of stock codes (e.g., ["sz000063"])
        init_cash: Initial capital (default 100000)
        frequency: Backtest frequency - "daily", "weekly", or "monthly" (default "daily")

    Returns:
        Backtest results with strategy return, Sharpe ratio, max drawdown, etc.

    Example:
        run_backtest(
            strategy_code="...",
            start_date="2020-01-01",
            end_date="2020-03-31",
            stocks=["sz000063"]
        )
    """
    try:
        # Prepare request payload
        payload = {
            "start": start_date,
            "end": end_date,
            "stocks": stocks,
            "init_cash": init_cash,
            "frequency": frequency,
            "strategy_code": strategy_code,
        }

        # Call backtest API
        response = requests.post(
            "http://10.0.1.159:8001/backtest",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )

        response.raise_for_status()
        result = response.json()

        # Check if request was successful
        if result.get("status") != "success":
            return f"Backtest failed: {result.get('message', 'Unknown error')}"

        # Extract key metrics
        analysis = result.get("analysis", {})
        strategy_return = analysis.get("strategy_return", 0)
        benchmark_return = analysis.get("benchmark_return", 0)
        sharpe = analysis.get("sharpe", 0)
        max_drawdown = analysis.get("max_drawdown", 0)
        alpha = analysis.get("alpha", 0)
        beta = analysis.get("beta", 0)
        annual_return = analysis.get("annual_return", 0)
        volatility = analysis.get("volatility", 0)

        # Format output
        output = f"""Backtest Results ({start_date} to {end_date}):

Performance Metrics:
- Strategy Return: {strategy_return:.2%}
- Benchmark Return: {benchmark_return:.2%}
- Annualized Return: {annual_return:.2%}
- Sharpe Ratio: {sharpe:.2f}
- Max Drawdown: {max_drawdown:.2%}
- Volatility: {volatility:.2%}

Risk Metrics:
- Alpha: {alpha:.2f}
- Beta: {beta:.2f}

Strategy outperformed benchmark by {(strategy_return - benchmark_return):.2%}
"""

        return output

    except requests.exceptions.Timeout:
        return "Error: Backtest request timed out (>60s). Try a shorter time period."
    except requests.exceptions.RequestException as e:
        return f"Error: Failed to connect to backtest API: {str(e)}"
    except Exception as e:
        return f"Error running backtest: {str(e)}"


# Create tools (no API key needed - will be passed per-request)
# Only include search and read tools - no write/execution tools
tools = [
    tool
    for tool in get_nexus_tools()
    if tool.name
    in ["grep_files", "glob_files", "read_file", "web_search", "write_file", "web_crawl"]
]
# Add backtest tool
tools.append(run_backtest)

# Create LLM
llm = ChatAnthropic(
    model="claude-sonnet-4-5-20250929",
    max_tokens=10000,
)

# One-shot example: Dual Moving Average Strategy for qlib
QLIB_STRATEGY_EXAMPLE = """import pandas as pd
import numpy as np

class UserStrategy:
    def generate_signals(self, data):
        df = data.copy()

        # Handle MultiIndex
        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index()
            if 'datetime' in df.columns:
                df['datetime'] = pd.to_datetime(df['datetime'])
                df.set_index('datetime', inplace=True)

        df.index = pd.to_datetime(df.index)

        # Dual moving average strategy
        if '$close' in df.columns:
            short_window = 5
            long_window = 20

            df['short_ma'] = df['$close'].rolling(short_window).mean()
            df['long_ma'] = df['$close'].rolling(long_window).mean()

            # Golden cross buy, death cross sell
            df['signal'] = 0
            df.loc[df['short_ma'] > df['long_ma'], 'signal'] = 1

        result = pd.DataFrame({'signal': df['signal']}, index=df.index)
        result['signal'] = result['signal'].fillna(0).astype(int)

        return result"""

# System prompt for Quantitative Trading Strategy Development
# Extends the official CODING_AGENT_SYSTEM_PROMPT with domain-specific instructions
SYSTEM_PROMPT = (
    CODING_AGENT_SYSTEM_PROMPT
    + f"""

## Quantitative Trading Specialization

You specialize in quantitative trading strategies including:
- Mean reversion, momentum, statistical arbitrage, pairs trading
- Backtesting frameworks and risk management systems
- Portfolio optimization and performance analysis

## Additional Tools

Beyond the standard Nexus tools, you also have:
- `run_backtest(strategy_code, start_date, end_date, stocks, init_cash, frequency)`: Run backtest for qlib strategies
- `web_search(query)`: Search web for current information
- `web_crawl(url)`: Fetch web page content as markdown

## Quantitative Workflow

1. **Research API docs** - Search for qlib/qmt_api documentation using grep_files/glob_files
2. **Read examples** - Study API references and existing strategies with read_file
3. **Web research** - Use web_search/web_crawl for additional references if needed
4. **Return code** - Provide complete, executable Python code in your response

## Response Format

**ALWAYS return Python code directly in your response.** Do NOT write files unless explicitly asked.

Your response should contain:
1. **Code block** - Production-ready Python code with proper structure
2. **Explanation** - Brief description of the strategy and key design decisions

## Quantitative Best Practices

- Search API docs in qlib/qmt_api folders before writing code
- Account for transaction costs and slippage in backtests
- Implement proper risk management (position sizing, stop-loss, portfolio limits)
- Calculate key metrics: Sharpe ratio, max drawdown, win rate, profit factor
- Write modular, reusable strategy classes

## One-Shot Example: Dual Moving Average Strategy

Here's an example of a well-structured qlib strategy:

```python
{QLIB_STRATEGY_EXAMPLE}
```

Be analytical, rigorous, and return clean, executable code in every response!"""
)


def build_prompt(state: dict, config: RunnableConfig) -> list:  # noqa: ARG001
    """Build prompt with optional opened file context from metadata.

    This function is called before each LLM invocation and can access
    the config which includes metadata from the frontend.
    """
    # Build system prompt with optional context
    system_content = SYSTEM_PROMPT

    # Return system message + user messages
    return [SystemMessage(content=system_content)] + state["messages"]


# Create a runnable that wraps the prompt builder
prompt_runnable = RunnableLambda(build_prompt)

# Create prebuilt ReAct agent with dynamic prompt
agent = create_react_agent(
    model=llm,
    tools=tools,
    prompt=prompt_runnable,
)


if __name__ == "__main__":
    # Example usage - Note: requires NEXUS_API_KEY to be set for testing
    import sys

    api_key = os.getenv("NEXUS_API_KEY")
    if not api_key:
        print("Error: NEXUS_API_KEY environment variable is required for testing")
        print("Usage: NEXUS_API_KEY=your-key python quant_agent.py")
        sys.exit(1)

    print("Testing Quant Agent...")

    # Test with quant strategy request
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Search for API documentation about fetching historical price data, then write a simple mean reversion strategy.",
                }
            ]
        },
        config={
            "metadata": {
                "x_auth": f"Bearer {api_key}",
                "opened_file_path": "/workspace/admin/strategies/mean_reversion.py",  # Optional: simulates opened file
            }
        },
    )
    print(result)
