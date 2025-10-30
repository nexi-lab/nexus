"""Reflection system for analyzing trajectories and extracting learnings."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from nexus.core.ace.trajectory import TrajectoryManager
from nexus.llm.message import Message, MessageRole
from nexus.llm.provider import LLMProvider
from nexus.storage.models import MemoryModel


class Reflector:
    """Analyze execution trajectories to extract learnings.

    Uses LLM to reflect on what worked, what didn't, and why.
    Generates structured insights for playbook curation.
    """

    def __init__(
        self,
        session: Session,
        backend: Any,
        llm_provider: LLMProvider,
        trajectory_manager: TrajectoryManager,
        user_id: str,
        agent_id: str | None = None,
        tenant_id: str | None = None,
    ):
        """Initialize reflector.

        Args:
            session: Database session
            backend: Storage backend for CAS content
            llm_provider: LLM provider for reflection analysis
            trajectory_manager: Trajectory manager for reading trajectories
            user_id: User ID for ownership
            agent_id: Optional agent ID
            tenant_id: Optional tenant ID
        """
        self.session = session
        self.backend = backend
        self.llm_provider = llm_provider
        self.trajectory_manager = trajectory_manager
        self.user_id = user_id
        self.agent_id = agent_id
        self.tenant_id = tenant_id

    async def reflect_async(
        self,
        trajectory_id: str,
        context: str | None = None,
        reflection_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Analyze a trajectory and extract learnings (async).

        Args:
            trajectory_id: Trajectory ID to reflect on
            context: Optional additional context
            reflection_prompt: Optional custom reflection prompt

        Returns:
            Dictionary with reflection results:
                - helpful_strategies: List of successful patterns
                - harmful_patterns: List of failure patterns
                - observations: Neutral observations
                - confidence: Confidence score (0.0-1.0)
                - memory_id: ID of stored reflection memory

        Example:
            >>> reflection = await reflector.reflect_async(trajectory_id)
            >>> for strategy in reflection['helpful_strategies']:
            ...     print(f"✓ {strategy['description']}")
        """
        # Get trajectory data
        trajectory_data = self.trajectory_manager.get_trajectory(trajectory_id)
        if not trajectory_data:
            raise ValueError(f"Trajectory {trajectory_id} not found")

        # Build reflection prompt
        prompt = reflection_prompt or self._build_reflection_prompt(trajectory_data, context)

        # Call LLM for analysis
        messages = [Message(role=MessageRole.USER, content=prompt)]

        response = await self.llm_provider.complete_async(messages)
        analysis_text = response.content

        # Parse LLM response into structured format
        reflection_data = self._parse_reflection_response(analysis_text, trajectory_data)

        # Store reflection as memory
        memory_id = self._store_reflection(trajectory_id, reflection_data)

        return {
            "memory_id": memory_id,
            "trajectory_id": trajectory_id,
            "helpful_strategies": reflection_data.get("helpful_strategies", []),
            "harmful_patterns": reflection_data.get("harmful_patterns", []),
            "observations": reflection_data.get("observations", []),
            "confidence": reflection_data.get("confidence", 0.5),
            "analysis": analysis_text,
        }

    def _build_reflection_prompt(self, trajectory_data: dict[str, Any], context: str | None) -> str:
        """Build reflection prompt for LLM analysis.

        Args:
            trajectory_data: Trajectory data including trace
            context: Optional additional context

        Returns:
            Formatted prompt string
        """
        task_desc = trajectory_data["task_description"]
        status = trajectory_data["status"]
        success_score = trajectory_data.get("success_score", 0.0)
        trace = trajectory_data.get("trace", {})

        # Extract key information from trace
        steps = trace.get("steps", [])
        decisions = trace.get("decisions", [])
        observations = trace.get("observations", [])

        prompt = f"""# Trajectory Reflection Analysis

You are an expert at analyzing agent execution trajectories to extract learnings.

## Task
{task_desc}

## Outcome
- Status: {status}
- Success Score: {success_score:.2f}

## Execution Trace

### Steps Taken ({len(steps)} steps)
{self._format_trace_items(steps[:10])}

### Decisions Made ({len(decisions)} decisions)
{self._format_trace_items(decisions[:10])}

### Observations ({len(observations)} observations)
{self._format_trace_items(observations[:10])}
"""

        if context:
            prompt += f"\n## Additional Context\n{context}\n"

        prompt += """
## Your Task

Analyze this trajectory and extract structured learnings in JSON format:

```json
{
  "helpful_strategies": [
    {
      "description": "Clear description of what worked well",
      "evidence": "Why this was effective",
      "confidence": 0.9
    }
  ],
  "harmful_patterns": [
    {
      "description": "Clear description of what went wrong",
      "evidence": "Why this was problematic",
      "impact": "What negative effect it had",
      "confidence": 0.85
    }
  ],
  "observations": [
    {
      "description": "Neutral observation about the execution",
      "relevance": "Why this might be useful to know"
    }
  ],
  "confidence": 0.8
}
```

Guidelines:
- Be specific and actionable
- Focus on generalizable patterns, not one-off incidents
- Use evidence from the trace to support each insight
- Mark strategies with high confidence (>0.8) as most reliable
- Distinguish between helpful strategies (✓), harmful patterns (✗), and neutral observations (○)

Provide your analysis as valid JSON only, no additional commentary.
"""

        return prompt

    def _format_trace_items(self, items: list[dict[str, Any]]) -> str:
        """Format trace items for prompt.

        Args:
            items: List of trace items

        Returns:
            Formatted string
        """
        if not items:
            return "(none)"

        formatted = []
        for i, item in enumerate(items, 1):
            desc = item.get("description", "")
            result = item.get("result", "")
            if result:
                formatted.append(f"{i}. {desc} → {result}")
            else:
                formatted.append(f"{i}. {desc}")

        return "\n".join(formatted)

    def _parse_reflection_response(
        self,
        analysis_text: str | None,
        trajectory_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Parse LLM reflection response into structured format.

        Args:
            analysis_text: LLM response text
            trajectory_data: Original trajectory data

        Returns:
            Structured reflection data
        """
        if not analysis_text:
            # Fallback: create basic reflection from trajectory data
            return self._create_fallback_reflection(trajectory_data)

        try:
            # Try to extract JSON from response
            # Look for JSON code block
            if "```json" in analysis_text:
                json_start = analysis_text.find("```json") + 7
                json_end = analysis_text.find("```", json_start)
                json_str = analysis_text[json_start:json_end].strip()
            elif "```" in analysis_text:
                json_start = analysis_text.find("```") + 3
                json_end = analysis_text.find("```", json_start)
                json_str = analysis_text[json_start:json_end].strip()
            else:
                # Assume entire response is JSON
                json_str = analysis_text.strip()

            reflection_data: dict[str, Any] = json.loads(json_str)
            return reflection_data

        except (json.JSONDecodeError, ValueError):
            # Fallback if parsing fails
            return self._create_fallback_reflection(trajectory_data)

    def _create_fallback_reflection(self, trajectory_data: dict[str, Any]) -> dict[str, Any]:
        """Create fallback reflection when LLM parsing fails.

        Args:
            trajectory_data: Trajectory data

        Returns:
            Basic reflection structure
        """
        success_score = trajectory_data.get("success_score", 0.0)
        status = trajectory_data["status"]
        trace = trajectory_data.get("trace", {})

        reflection: dict[str, Any] = {
            "helpful_strategies": [],
            "harmful_patterns": [],
            "observations": [],
            "confidence": 0.4,  # Low-medium confidence for fallback
        }
        helpful_strategies: list[dict[str, Any]] = reflection["helpful_strategies"]
        harmful_patterns: list[dict[str, Any]] = reflection["harmful_patterns"]

        # Extract insights from trace observations and steps
        observations = trace.get("observations", [])

        # Look for error detection patterns in observations
        for obs in observations:
            result = obs.get("result")

            # Extract error types found
            if result and isinstance(result, dict):
                error_types = result.get("error_types", {})
                if error_types:
                    # Create strategies for successfully detected errors
                    # Map error types to validation strategy descriptions
                    for error_type, count in error_types.items():
                        strategy_desc = None
                        if "age" in error_type.lower():
                            strategy_desc = (
                                "Validate age values: check for missing age and age range 0-100"
                            )
                        elif "sex" in error_type.lower():
                            strategy_desc = (
                                "Check sex field: validate categorical values (male/female)"
                            )
                        elif "fare" in error_type.lower():
                            strategy_desc = "Validate fare: check non-negative values"
                        elif "missing" in error_type.lower():
                            strategy_desc = (
                                "Check required fields: validate Name, Age, Sex are present"
                            )
                        else:
                            strategy_desc = (
                                f"Validate data quality: check for {error_type.replace('_', ' ')}"
                            )

                        helpful_strategies.append(
                            {
                                "description": strategy_desc,
                                "evidence": f"Successfully detected {count} instances of {error_type}",
                                "confidence": 0.75,
                            }
                        )

                # Extract missed error patterns
                samples = result.get("samples", [])
                if samples:
                    # Extract missed error types
                    missed_reasons = set()
                    for sample in samples:
                        for reason in sample.get("missed_reasons", []):
                            missed_reasons.add(reason)

                    for reason in missed_reasons:
                        # Create learning suggestions for missed validations
                        suggestion = None
                        if "age" in reason.lower():
                            suggestion = (
                                "Need to add age validation: check missing values and range"
                            )
                        elif "sex" in reason.lower():
                            suggestion = "Need to add sex validation: check categorical values"
                        elif "fare" in reason.lower():
                            suggestion = "Need to add fare validation: check for negative values"
                        elif "missing" in reason.lower():
                            suggestion = "Need to check required fields more thoroughly"
                        else:
                            suggestion = f"Missing validation for {reason.replace('_', ' ')}"

                        harmful_patterns.append(
                            {
                                "description": suggestion,
                                "evidence": f"Missed {reason} errors in {len(samples)} samples",
                                "impact": "Lower detection accuracy",
                                "confidence": 0.75,
                            }
                        )

        # Generic success/failure patterns if no specific insights
        if not helpful_strategies and not harmful_patterns:
            if status == "success" and success_score > 0.7:
                helpful_strategies.append(
                    {
                        "description": f"Successfully completed: {trajectory_data['task_description']}",
                        "evidence": f"Achieved success score of {success_score:.2f}",
                        "confidence": 0.5,
                    }
                )
            elif status == "failure":
                harmful_patterns.append(
                    {
                        "description": f"Failed to complete: {trajectory_data['task_description']}",
                        "evidence": trajectory_data.get("error_message", "Unknown error"),
                        "impact": "Task did not complete successfully",
                        "confidence": 0.5,
                    }
                )

        return reflection

    def _store_reflection(self, trajectory_id: str, reflection_data: dict[str, Any]) -> str:
        """Store reflection as a memory.

        Args:
            trajectory_id: Trajectory ID
            reflection_data: Reflection data to store

        Returns:
            memory_id: ID of stored reflection memory
        """
        memory_id = str(uuid.uuid4())

        # Prepare reflection content
        content = {
            "type": "reflection",
            "trajectory_id": trajectory_id,
            "reflection": reflection_data,
            "created_at": datetime.now(UTC).isoformat(),
        }

        # Store in CAS
        content_json = json.dumps(content, indent=2).encode("utf-8")
        content_hash = self.backend.write_content(content_json)

        # Create memory record
        memory = MemoryModel(
            memory_id=memory_id,
            content_hash=content_hash,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            agent_id=self.agent_id,
            scope="agent",
            visibility="private",
            memory_type="reflection",
            trajectory_id=trajectory_id,
            importance=reflection_data.get("confidence", 0.5),
        )

        self.session.add(memory)
        self.session.commit()

        return memory_id

    def sync_reflect(
        self,
        trajectory_id: str,
        context: str | None = None,
        reflection_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Synchronous wrapper for reflect_async.

        Args:
            trajectory_id: Trajectory ID to reflect on
            context: Optional additional context
            reflection_prompt: Optional custom reflection prompt

        Returns:
            Reflection results
        """
        import asyncio

        return asyncio.run(self.reflect_async(trajectory_id, context, reflection_prompt))
