from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class QueryType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    QUERY_TYPE_UNSPECIFIED: _ClassVar[QueryType]
    QUERY_TYPE_KEYWORD: _ClassVar[QueryType]
    QUERY_TYPE_SEMANTIC: _ClassVar[QueryType]
    QUERY_TYPE_HYBRID: _ClassVar[QueryType]

class FusionMethod(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FUSION_METHOD_UNSPECIFIED: _ClassVar[FusionMethod]
    FUSION_METHOD_RRF: _ClassVar[FusionMethod]
    FUSION_METHOD_WEIGHTED: _ClassVar[FusionMethod]
    FUSION_METHOD_RRF_WEIGHTED: _ClassVar[FusionMethod]
QUERY_TYPE_UNSPECIFIED: QueryType
QUERY_TYPE_KEYWORD: QueryType
QUERY_TYPE_SEMANTIC: QueryType
QUERY_TYPE_HYBRID: QueryType
FUSION_METHOD_UNSPECIFIED: FusionMethod
FUSION_METHOD_RRF: FusionMethod
FUSION_METHOD_WEIGHTED: FusionMethod
FUSION_METHOD_RRF_WEIGHTED: FusionMethod

class GlobRequest(_message.Message):
    __slots__ = ("root_path", "pattern", "max_results", "auth_token", "sort_recency")
    ROOT_PATH_FIELD_NUMBER: _ClassVar[int]
    PATTERN_FIELD_NUMBER: _ClassVar[int]
    MAX_RESULTS_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    SORT_RECENCY_FIELD_NUMBER: _ClassVar[int]
    root_path: str
    pattern: str
    max_results: int
    auth_token: str
    sort_recency: bool
    def __init__(self, root_path: _Optional[str] = ..., pattern: _Optional[str] = ..., max_results: _Optional[int] = ..., auth_token: _Optional[str] = ..., sort_recency: bool = ...) -> None: ...

class GlobResponse(_message.Message):
    __slots__ = ("paths", "truncated", "error")
    PATHS_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    paths: _containers.RepeatedScalarFieldContainer[str]
    truncated: bool
    error: str
    def __init__(self, paths: _Optional[_Iterable[str]] = ..., truncated: bool = ..., error: _Optional[str] = ...) -> None: ...

class GrepRequest(_message.Message):
    __slots__ = ("root_path", "pattern", "file_pattern", "ignore_case", "max_results", "before_context", "after_context", "invert_match", "auth_token", "sort_recency")
    ROOT_PATH_FIELD_NUMBER: _ClassVar[int]
    PATTERN_FIELD_NUMBER: _ClassVar[int]
    FILE_PATTERN_FIELD_NUMBER: _ClassVar[int]
    IGNORE_CASE_FIELD_NUMBER: _ClassVar[int]
    MAX_RESULTS_FIELD_NUMBER: _ClassVar[int]
    BEFORE_CONTEXT_FIELD_NUMBER: _ClassVar[int]
    AFTER_CONTEXT_FIELD_NUMBER: _ClassVar[int]
    INVERT_MATCH_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    SORT_RECENCY_FIELD_NUMBER: _ClassVar[int]
    root_path: str
    pattern: str
    file_pattern: str
    ignore_case: bool
    max_results: int
    before_context: int
    after_context: int
    invert_match: bool
    auth_token: str
    sort_recency: bool
    def __init__(self, root_path: _Optional[str] = ..., pattern: _Optional[str] = ..., file_pattern: _Optional[str] = ..., ignore_case: bool = ..., max_results: _Optional[int] = ..., before_context: _Optional[int] = ..., after_context: _Optional[int] = ..., invert_match: bool = ..., auth_token: _Optional[str] = ..., sort_recency: bool = ...) -> None: ...

class GrepMatch(_message.Message):
    __slots__ = ("path", "line_number", "line", "before", "after")
    PATH_FIELD_NUMBER: _ClassVar[int]
    LINE_NUMBER_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    BEFORE_FIELD_NUMBER: _ClassVar[int]
    AFTER_FIELD_NUMBER: _ClassVar[int]
    path: str
    line_number: int
    line: str
    before: _containers.RepeatedScalarFieldContainer[str]
    after: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, path: _Optional[str] = ..., line_number: _Optional[int] = ..., line: _Optional[str] = ..., before: _Optional[_Iterable[str]] = ..., after: _Optional[_Iterable[str]] = ...) -> None: ...

class GrepResponse(_message.Message):
    __slots__ = ("matches", "truncated", "error")
    MATCHES_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    matches: _containers.RepeatedCompositeFieldContainer[GrepMatch]
    truncated: bool
    error: str
    def __init__(self, matches: _Optional[_Iterable[_Union[GrepMatch, _Mapping]]] = ..., truncated: bool = ..., error: _Optional[str] = ...) -> None: ...

class QueryRequest(_message.Message):
    __slots__ = ("q", "zone_id", "limit", "path_filter", "query_type", "auth_token", "alpha", "fusion_method", "rrf_k", "chunks_per_page", "expand", "recency_mode", "recency_weight", "recency_half_life_days", "path_prefix_boosts")
    class PathPrefixBoostsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: float
        def __init__(self, key: _Optional[str] = ..., value: _Optional[float] = ...) -> None: ...
    Q_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    PATH_FILTER_FIELD_NUMBER: _ClassVar[int]
    QUERY_TYPE_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    ALPHA_FIELD_NUMBER: _ClassVar[int]
    FUSION_METHOD_FIELD_NUMBER: _ClassVar[int]
    RRF_K_FIELD_NUMBER: _ClassVar[int]
    CHUNKS_PER_PAGE_FIELD_NUMBER: _ClassVar[int]
    EXPAND_FIELD_NUMBER: _ClassVar[int]
    RECENCY_MODE_FIELD_NUMBER: _ClassVar[int]
    RECENCY_WEIGHT_FIELD_NUMBER: _ClassVar[int]
    RECENCY_HALF_LIFE_DAYS_FIELD_NUMBER: _ClassVar[int]
    PATH_PREFIX_BOOSTS_FIELD_NUMBER: _ClassVar[int]
    q: str
    zone_id: str
    limit: int
    path_filter: str
    query_type: QueryType
    auth_token: str
    alpha: float
    fusion_method: FusionMethod
    rrf_k: int
    chunks_per_page: int
    expand: str
    recency_mode: str
    recency_weight: float
    recency_half_life_days: float
    path_prefix_boosts: _containers.ScalarMap[str, float]
    def __init__(self, q: _Optional[str] = ..., zone_id: _Optional[str] = ..., limit: _Optional[int] = ..., path_filter: _Optional[str] = ..., query_type: _Optional[_Union[QueryType, str]] = ..., auth_token: _Optional[str] = ..., alpha: _Optional[float] = ..., fusion_method: _Optional[_Union[FusionMethod, str]] = ..., rrf_k: _Optional[int] = ..., chunks_per_page: _Optional[int] = ..., expand: _Optional[str] = ..., recency_mode: _Optional[str] = ..., recency_weight: _Optional[float] = ..., recency_half_life_days: _Optional[float] = ..., path_prefix_boosts: _Optional[_Mapping[str, float]] = ...) -> None: ...

class QueryResult(_message.Message):
    __slots__ = ("path", "chunk_index", "chunk_text", "score", "zone_id", "mtime_ms", "expanded_context")
    PATH_FIELD_NUMBER: _ClassVar[int]
    CHUNK_INDEX_FIELD_NUMBER: _ClassVar[int]
    CHUNK_TEXT_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    MTIME_MS_FIELD_NUMBER: _ClassVar[int]
    EXPANDED_CONTEXT_FIELD_NUMBER: _ClassVar[int]
    path: str
    chunk_index: int
    chunk_text: str
    score: float
    zone_id: str
    mtime_ms: int
    expanded_context: str
    def __init__(self, path: _Optional[str] = ..., chunk_index: _Optional[int] = ..., chunk_text: _Optional[str] = ..., score: _Optional[float] = ..., zone_id: _Optional[str] = ..., mtime_ms: _Optional[int] = ..., expanded_context: _Optional[str] = ...) -> None: ...

class QueryResponse(_message.Message):
    __slots__ = ("results", "error")
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    results: _containers.RepeatedCompositeFieldContainer[QueryResult]
    error: str
    def __init__(self, results: _Optional[_Iterable[_Union[QueryResult, _Mapping]]] = ..., error: _Optional[str] = ...) -> None: ...

class IndexRequest(_message.Message):
    __slots__ = ("root_path", "zone_id", "recursive", "max_docs", "auth_token")
    ROOT_PATH_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    RECURSIVE_FIELD_NUMBER: _ClassVar[int]
    MAX_DOCS_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    root_path: str
    zone_id: str
    recursive: bool
    max_docs: int
    auth_token: str
    def __init__(self, root_path: _Optional[str] = ..., zone_id: _Optional[str] = ..., recursive: bool = ..., max_docs: _Optional[int] = ..., auth_token: _Optional[str] = ...) -> None: ...

class IndexResponse(_message.Message):
    __slots__ = ("indexed_count", "skipped_count", "error")
    INDEXED_COUNT_FIELD_NUMBER: _ClassVar[int]
    SKIPPED_COUNT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    indexed_count: int
    skipped_count: int
    error: str
    def __init__(self, indexed_count: _Optional[int] = ..., skipped_count: _Optional[int] = ..., error: _Optional[str] = ...) -> None: ...

class RefreshRequest(_message.Message):
    __slots__ = ("root_path", "zone_id", "recursive", "max_docs", "auth_token")
    ROOT_PATH_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    RECURSIVE_FIELD_NUMBER: _ClassVar[int]
    MAX_DOCS_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    root_path: str
    zone_id: str
    recursive: bool
    max_docs: int
    auth_token: str
    def __init__(self, root_path: _Optional[str] = ..., zone_id: _Optional[str] = ..., recursive: bool = ..., max_docs: _Optional[int] = ..., auth_token: _Optional[str] = ...) -> None: ...

class RefreshResponse(_message.Message):
    __slots__ = ("reindexed_count", "removed_count", "unchanged_count", "skipped_count", "error")
    REINDEXED_COUNT_FIELD_NUMBER: _ClassVar[int]
    REMOVED_COUNT_FIELD_NUMBER: _ClassVar[int]
    UNCHANGED_COUNT_FIELD_NUMBER: _ClassVar[int]
    SKIPPED_COUNT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    reindexed_count: int
    removed_count: int
    unchanged_count: int
    skipped_count: int
    error: str
    def __init__(self, reindexed_count: _Optional[int] = ..., removed_count: _Optional[int] = ..., unchanged_count: _Optional[int] = ..., skipped_count: _Optional[int] = ..., error: _Optional[str] = ...) -> None: ...

class BatchQueryRequest(_message.Message):
    __slots__ = ("queries", "auth_token")
    QUERIES_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    queries: _containers.RepeatedCompositeFieldContainer[QueryRequest]
    auth_token: str
    def __init__(self, queries: _Optional[_Iterable[_Union[QueryRequest, _Mapping]]] = ..., auth_token: _Optional[str] = ...) -> None: ...

class BatchQueryResponse(_message.Message):
    __slots__ = ("responses",)
    RESPONSES_FIELD_NUMBER: _ClassVar[int]
    responses: _containers.RepeatedCompositeFieldContainer[QueryResponse]
    def __init__(self, responses: _Optional[_Iterable[_Union[QueryResponse, _Mapping]]] = ...) -> None: ...

class DocumentInput(_message.Message):
    __slots__ = ("path", "text", "mtime_ms", "zone_id")
    PATH_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    MTIME_MS_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    path: str
    text: str
    mtime_ms: int
    zone_id: str
    def __init__(self, path: _Optional[str] = ..., text: _Optional[str] = ..., mtime_ms: _Optional[int] = ..., zone_id: _Optional[str] = ...) -> None: ...

class IndexDocumentsRequest(_message.Message):
    __slots__ = ("documents", "zone_id", "auth_token")
    DOCUMENTS_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    documents: _containers.RepeatedCompositeFieldContainer[DocumentInput]
    zone_id: str
    auth_token: str
    def __init__(self, documents: _Optional[_Iterable[_Union[DocumentInput, _Mapping]]] = ..., zone_id: _Optional[str] = ..., auth_token: _Optional[str] = ...) -> None: ...

class IndexDocumentsResponse(_message.Message):
    __slots__ = ("indexed_count", "skipped_count", "parked_paths", "error")
    INDEXED_COUNT_FIELD_NUMBER: _ClassVar[int]
    SKIPPED_COUNT_FIELD_NUMBER: _ClassVar[int]
    PARKED_PATHS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    indexed_count: int
    skipped_count: int
    parked_paths: _containers.RepeatedScalarFieldContainer[str]
    error: str
    def __init__(self, indexed_count: _Optional[int] = ..., skipped_count: _Optional[int] = ..., parked_paths: _Optional[_Iterable[str]] = ..., error: _Optional[str] = ...) -> None: ...

class NotifyFileChangeRequest(_message.Message):
    __slots__ = ("path", "change_type", "zone_id", "auth_token")
    PATH_FIELD_NUMBER: _ClassVar[int]
    CHANGE_TYPE_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    path: str
    change_type: str
    zone_id: str
    auth_token: str
    def __init__(self, path: _Optional[str] = ..., change_type: _Optional[str] = ..., zone_id: _Optional[str] = ..., auth_token: _Optional[str] = ...) -> None: ...

class NotifyFileChangeResponse(_message.Message):
    __slots__ = ("status", "error")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    status: str
    error: str
    def __init__(self, status: _Optional[str] = ..., error: _Optional[str] = ...) -> None: ...

class LocateRequest(_message.Message):
    __slots__ = ("path", "zone_id", "auth_token")
    PATH_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    path: str
    zone_id: str
    auth_token: str
    def __init__(self, path: _Optional[str] = ..., zone_id: _Optional[str] = ..., auth_token: _Optional[str] = ...) -> None: ...

class LocateResponse(_message.Message):
    __slots__ = ("indexed", "chunk_count", "mtime_ms", "zone_id")
    INDEXED_FIELD_NUMBER: _ClassVar[int]
    CHUNK_COUNT_FIELD_NUMBER: _ClassVar[int]
    MTIME_MS_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    indexed: bool
    chunk_count: int
    mtime_ms: int
    zone_id: str
    def __init__(self, indexed: bool = ..., chunk_count: _Optional[int] = ..., mtime_ms: _Optional[int] = ..., zone_id: _Optional[str] = ...) -> None: ...

class ParkedEntry(_message.Message):
    __slots__ = ("path", "zone_id", "parked_at_ms", "reason")
    PATH_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    PARKED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    path: str
    zone_id: str
    parked_at_ms: int
    reason: str
    def __init__(self, path: _Optional[str] = ..., zone_id: _Optional[str] = ..., parked_at_ms: _Optional[int] = ..., reason: _Optional[str] = ...) -> None: ...

class ParkedListRequest(_message.Message):
    __slots__ = ("zone_id", "auth_token")
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    zone_id: str
    auth_token: str
    def __init__(self, zone_id: _Optional[str] = ..., auth_token: _Optional[str] = ...) -> None: ...

class ParkedListResponse(_message.Message):
    __slots__ = ("entries", "error")
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[ParkedEntry]
    error: str
    def __init__(self, entries: _Optional[_Iterable[_Union[ParkedEntry, _Mapping]]] = ..., error: _Optional[str] = ...) -> None: ...

class ParkedRetryRequest(_message.Message):
    __slots__ = ("paths", "zone_id", "auth_token")
    PATHS_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    paths: _containers.RepeatedScalarFieldContainer[str]
    zone_id: str
    auth_token: str
    def __init__(self, paths: _Optional[_Iterable[str]] = ..., zone_id: _Optional[str] = ..., auth_token: _Optional[str] = ...) -> None: ...

class ParkedRetryResponse(_message.Message):
    __slots__ = ("retried_count", "still_parked_count", "error")
    RETRIED_COUNT_FIELD_NUMBER: _ClassVar[int]
    STILL_PARKED_COUNT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    retried_count: int
    still_parked_count: int
    error: str
    def __init__(self, retried_count: _Optional[int] = ..., still_parked_count: _Optional[int] = ..., error: _Optional[str] = ...) -> None: ...

class ParkedDiscardRequest(_message.Message):
    __slots__ = ("paths", "zone_id", "auth_token")
    PATHS_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    paths: _containers.RepeatedScalarFieldContainer[str]
    zone_id: str
    auth_token: str
    def __init__(self, paths: _Optional[_Iterable[str]] = ..., zone_id: _Optional[str] = ..., auth_token: _Optional[str] = ...) -> None: ...

class ParkedDiscardResponse(_message.Message):
    __slots__ = ("discarded_count", "error")
    DISCARDED_COUNT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    discarded_count: int
    error: str
    def __init__(self, discarded_count: _Optional[int] = ..., error: _Optional[str] = ...) -> None: ...

class IndexedDirectory(_message.Message):
    __slots__ = ("path", "zone_id", "added_at_ms")
    PATH_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    ADDED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    path: str
    zone_id: str
    added_at_ms: int
    def __init__(self, path: _Optional[str] = ..., zone_id: _Optional[str] = ..., added_at_ms: _Optional[int] = ...) -> None: ...

class AddIndexedDirectoryRequest(_message.Message):
    __slots__ = ("path", "zone_id", "auth_token")
    PATH_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    path: str
    zone_id: str
    auth_token: str
    def __init__(self, path: _Optional[str] = ..., zone_id: _Optional[str] = ..., auth_token: _Optional[str] = ...) -> None: ...

class AddIndexedDirectoryResponse(_message.Message):
    __slots__ = ("added", "error")
    ADDED_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    added: bool
    error: str
    def __init__(self, added: bool = ..., error: _Optional[str] = ...) -> None: ...

class RemoveIndexedDirectoryRequest(_message.Message):
    __slots__ = ("path", "zone_id", "auth_token")
    PATH_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    path: str
    zone_id: str
    auth_token: str
    def __init__(self, path: _Optional[str] = ..., zone_id: _Optional[str] = ..., auth_token: _Optional[str] = ...) -> None: ...

class RemoveIndexedDirectoryResponse(_message.Message):
    __slots__ = ("removed", "error")
    REMOVED_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    removed: bool
    error: str
    def __init__(self, removed: bool = ..., error: _Optional[str] = ...) -> None: ...

class ListIndexedDirectoriesRequest(_message.Message):
    __slots__ = ("zone_id", "auth_token")
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    zone_id: str
    auth_token: str
    def __init__(self, zone_id: _Optional[str] = ..., auth_token: _Optional[str] = ...) -> None: ...

class ListIndexedDirectoriesResponse(_message.Message):
    __slots__ = ("directories", "error")
    DIRECTORIES_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    directories: _containers.RepeatedCompositeFieldContainer[IndexedDirectory]
    error: str
    def __init__(self, directories: _Optional[_Iterable[_Union[IndexedDirectory, _Mapping]]] = ..., error: _Optional[str] = ...) -> None: ...

class SetZoneIndexingModeRequest(_message.Message):
    __slots__ = ("zone_id", "mode", "auth_token")
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    zone_id: str
    mode: str
    auth_token: str
    def __init__(self, zone_id: _Optional[str] = ..., mode: _Optional[str] = ..., auth_token: _Optional[str] = ...) -> None: ...

class SetZoneIndexingModeResponse(_message.Message):
    __slots__ = ("error",)
    ERROR_FIELD_NUMBER: _ClassVar[int]
    error: str
    def __init__(self, error: _Optional[str] = ...) -> None: ...

class ZoneIndexingMode(_message.Message):
    __slots__ = ("zone_id", "mode")
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    zone_id: str
    mode: str
    def __init__(self, zone_id: _Optional[str] = ..., mode: _Optional[str] = ...) -> None: ...

class ListZoneIndexingModesRequest(_message.Message):
    __slots__ = ("auth_token",)
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    auth_token: str
    def __init__(self, auth_token: _Optional[str] = ...) -> None: ...

class ListZoneIndexingModesResponse(_message.Message):
    __slots__ = ("modes", "error")
    MODES_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    modes: _containers.RepeatedCompositeFieldContainer[ZoneIndexingMode]
    error: str
    def __init__(self, modes: _Optional[_Iterable[_Union[ZoneIndexingMode, _Mapping]]] = ..., error: _Optional[str] = ...) -> None: ...

class HealthRequest(_message.Message):
    __slots__ = ("auth_token",)
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    auth_token: str
    def __init__(self, auth_token: _Optional[str] = ...) -> None: ...

class HealthResponse(_message.Message):
    __slots__ = ("status", "detail")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    status: str
    detail: str
    def __init__(self, status: _Optional[str] = ..., detail: _Optional[str] = ...) -> None: ...

class StatsRequest(_message.Message):
    __slots__ = ("zone_id", "auth_token")
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    zone_id: str
    auth_token: str
    def __init__(self, zone_id: _Optional[str] = ..., auth_token: _Optional[str] = ...) -> None: ...

class StatsResponse(_message.Message):
    __slots__ = ("fts_doc_count", "fts_path_count", "ann_chunk_count", "parked_count", "error")
    FTS_DOC_COUNT_FIELD_NUMBER: _ClassVar[int]
    FTS_PATH_COUNT_FIELD_NUMBER: _ClassVar[int]
    ANN_CHUNK_COUNT_FIELD_NUMBER: _ClassVar[int]
    PARKED_COUNT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    fts_doc_count: int
    fts_path_count: int
    ann_chunk_count: int
    parked_count: int
    error: str
    def __init__(self, fts_doc_count: _Optional[int] = ..., fts_path_count: _Optional[int] = ..., ann_chunk_count: _Optional[int] = ..., parked_count: _Optional[int] = ..., error: _Optional[str] = ...) -> None: ...
