"""Search strategy enums and constants (Issue #929, #1520).

Backward-compat shim (Issue #2190): Canonical location is
``nexus.contracts.search_types``. This module re-exports for existing importers.

Issue #929: Adaptive algorithm selection for search operations.
Issue #1499: Shared query analysis patterns for query routing and expansion.
"""

from nexus.contracts.search_types import AGGREGATION_WORDS as AGGREGATION_WORDS  # noqa: F401
from nexus.contracts.search_types import COMPARISON_WORDS as COMPARISON_WORDS  # noqa: F401
from nexus.contracts.search_types import COMPLEX_PATTERNS as COMPLEX_PATTERNS  # noqa: F401
from nexus.contracts.search_types import GLOB_RUST_THRESHOLD as GLOB_RUST_THRESHOLD  # noqa: F401
from nexus.contracts.search_types import (  # noqa: F401
    GREP_CACHED_TEXT_RATIO as GREP_CACHED_TEXT_RATIO,
)
from nexus.contracts.search_types import (  # noqa: F401
    GREP_PARALLEL_THRESHOLD as GREP_PARALLEL_THRESHOLD,
)
from nexus.contracts.search_types import (  # noqa: F401
    GREP_PARALLEL_WORKERS as GREP_PARALLEL_WORKERS,
)
from nexus.contracts.search_types import (  # noqa: F401
    GREP_SEQUENTIAL_THRESHOLD as GREP_SEQUENTIAL_THRESHOLD,
)
from nexus.contracts.search_types import (  # noqa: F401
    GREP_TRIGRAM_THRESHOLD as GREP_TRIGRAM_THRESHOLD,
)
from nexus.contracts.search_types import (  # noqa: F401
    GREP_ZOEKT_THRESHOLD as GREP_ZOEKT_THRESHOLD,
)
from nexus.contracts.search_types import MULTIHOP_PATTERNS as MULTIHOP_PATTERNS  # noqa: F401
from nexus.contracts.search_types import TEMPORAL_WORDS as TEMPORAL_WORDS  # noqa: F401
from nexus.contracts.search_types import GlobStrategy as GlobStrategy  # noqa: F401
from nexus.contracts.search_types import SearchStrategy as SearchStrategy  # noqa: F401
