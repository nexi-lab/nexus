"""Adaptive retrieval depth protocol (Issue #2036).

Defines the contract for adaptive-k computation — dynamically adjusting the
number of search results based on query complexity.

The canonical implementation is ``ContextBuilder.calculate_k_dynamic()``
in ``nexus.services.llm.llm_context_builder``.  The search brick depends on
this protocol (not the concrete service) to satisfy LEGO Principle 3.

References:
    - Issue #1021: Adaptive retrieval depth (SimpleMem)
    - Issue #2036: Extract search module into search brick
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class AdaptiveKProtocol(Protocol):
    """Contract for adaptive retrieval depth computation.

    Implementations dynamically adjust the number of results (k) based on
    query complexity using the SimpleMem formula:
    ``k_dyn = floor(k_base * (1 + delta * C_q))``

    Used by the search brick to decouple from ``llm_context_builder``.
    """

    def calculate_k_dynamic(
        self,
        query: str,
        k_base: int | None = None,
    ) -> int:
        """Calculate adaptive retrieval depth for a query.

        Args:
            query: The search query to analyze.
            k_base: Base retrieval count (implementation uses its own default
                    if ``None``).

        Returns:
            Adjusted k value based on query complexity.
        """
        ...
