"""API v2 Pydantic models — split by domain for maintainability.

Re-exports every public model so existing imports continue to work:
    from nexus.server.api.v2.models import MemoryStoreRequest  # still works
"""

from nexus.server.api.v2.models.audit import (
    AuditAggregationResponse,
    AuditIntegrityResponse,
    AuditTransactionListResponse,
    AuditTransactionResponse,
)
from nexus.server.api.v2.models.base import ApiModel
from nexus.server.api.v2.models.conflicts import (
    ConflictDetailResponse,
    ConflictListResponse,
    ConflictResolveRequest,
    ConflictResolveResponse,
)
from nexus.server.api.v2.models.consolidation import (
    ConsolidateRequest,
    ConsolidationResponse,
    DecayRequest,
    DecayResponse,
    HierarchyBuildRequest,
    HierarchyResponse,
)
from nexus.server.api.v2.models.feedback import (
    FeedbackAddRequest,
    FeedbackAddResponse,
    FeedbackRelearnRequest,
    FeedbackResponse,
    FeedbackScoreRequest,
    FeedbackScoreResponse,
    TrajectoryFeedbackListResponse,
)
from nexus.server.api.v2.models.memories import (
    MemoryBatchStoreRequest,
    MemoryBatchStoreResponse,
    MemoryGetResponse,
    MemoryQueryRequest,
    MemoryResponse,
    MemorySearchRequest,
    MemoryStoreRequest,
    MemoryStoreResponse,
    MemoryUpdateRequest,
    MemoryVersionHistoryResponse,
)
from nexus.server.api.v2.models.operations import (
    AgentActivityResponse,
    OperationListResponse,
    OperationResponse,
)
from nexus.server.api.v2.models.playbooks import (
    PlaybookCreateRequest,
    PlaybookCreateResponse,
    PlaybookGetResponse,
    PlaybookResponse,
    PlaybookUpdateRequest,
    PlaybookUsageRequest,
)
from nexus.server.api.v2.models.reflection import (
    CurateBulkRequest,
    CurateRequest,
    CurationResponse,
    ReflectionResponse,
    ReflectRequest,
)
from nexus.server.api.v2.models.trajectories import (
    TrajectoryCompleteRequest,
    TrajectoryGetResponse,
    TrajectoryQueryParams,
    TrajectoryResponse,
    TrajectoryStartRequest,
    TrajectoryStartResponse,
    TrajectoryStepRequest,
)

__all__ = [
    # Base
    "ApiModel",
    # Memories
    "MemoryStoreRequest",
    "MemoryUpdateRequest",
    "MemorySearchRequest",
    "MemoryQueryRequest",
    "MemoryBatchStoreRequest",
    "MemoryResponse",
    "MemoryGetResponse",
    "MemoryStoreResponse",
    "MemoryBatchStoreResponse",
    "MemoryVersionHistoryResponse",
    # Trajectories
    "TrajectoryStartRequest",
    "TrajectoryStepRequest",
    "TrajectoryCompleteRequest",
    "TrajectoryQueryParams",
    "TrajectoryResponse",
    "TrajectoryGetResponse",
    "TrajectoryStartResponse",
    # Feedback
    "FeedbackAddRequest",
    "FeedbackScoreRequest",
    "FeedbackRelearnRequest",
    "FeedbackResponse",
    "TrajectoryFeedbackListResponse",
    "FeedbackAddResponse",
    "FeedbackScoreResponse",
    # Playbooks
    "PlaybookCreateRequest",
    "PlaybookUpdateRequest",
    "PlaybookUsageRequest",
    "PlaybookResponse",
    "PlaybookGetResponse",
    "PlaybookCreateResponse",
    # Reflection & Curation
    "ReflectRequest",
    "CurateRequest",
    "CurateBulkRequest",
    "ReflectionResponse",
    "CurationResponse",
    # Consolidation
    "ConsolidateRequest",
    "HierarchyBuildRequest",
    "DecayRequest",
    "ConsolidationResponse",
    "HierarchyResponse",
    "DecayResponse",
    # Conflicts
    "ConflictDetailResponse",
    "ConflictListResponse",
    "ConflictResolveRequest",
    "ConflictResolveResponse",
    # Operations
    "OperationResponse",
    "OperationListResponse",
    "AgentActivityResponse",
    # Audit
    "AuditTransactionResponse",
    "AuditTransactionListResponse",
    "AuditAggregationResponse",
    "AuditIntegrityResponse",
]
