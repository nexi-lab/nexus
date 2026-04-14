from typing import ClassVar as _ClassVar

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper

DESCRIPTOR: _descriptor.FileDescriptor

class DirEntryType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DT_REG: _ClassVar[DirEntryType]
    DT_DIR: _ClassVar[DirEntryType]
    DT_MOUNT: _ClassVar[DirEntryType]
    DT_PIPE: _ClassVar[DirEntryType]
    DT_STREAM: _ClassVar[DirEntryType]

DT_REG: DirEntryType
DT_DIR: DirEntryType
DT_MOUNT: DirEntryType
DT_PIPE: DirEntryType
DT_STREAM: DirEntryType

class FileMetadata(_message.Message):
    __slots__ = (
        "path",
        "backend_name",
        "physical_path",
        "size",
        "etag",
        "mime_type",
        "created_at",
        "modified_at",
        "version",
        "zone_id",
        "owner_id",
        "entry_type",
        "target_zone_id",
        "ttl_seconds",
    )
    PATH_FIELD_NUMBER: _ClassVar[int]
    BACKEND_NAME_FIELD_NUMBER: _ClassVar[int]
    PHYSICAL_PATH_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    ETAG_FIELD_NUMBER: _ClassVar[int]
    MIME_TYPE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    MODIFIED_AT_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    OWNER_ID_FIELD_NUMBER: _ClassVar[int]
    ENTRY_TYPE_FIELD_NUMBER: _ClassVar[int]
    TARGET_ZONE_ID_FIELD_NUMBER: _ClassVar[int]
    TTL_SECONDS_FIELD_NUMBER: _ClassVar[int]
    path: str
    backend_name: str
    physical_path: str
    size: int
    etag: str
    mime_type: str
    created_at: str
    modified_at: str
    version: int
    zone_id: str
    owner_id: str
    entry_type: DirEntryType
    target_zone_id: str
    ttl_seconds: float
    def __init__(
        self,
        path: str | None = ...,
        backend_name: str | None = ...,
        physical_path: str | None = ...,
        size: int | None = ...,
        etag: str | None = ...,
        mime_type: str | None = ...,
        created_at: str | None = ...,
        modified_at: str | None = ...,
        version: int | None = ...,
        zone_id: str | None = ...,
        owner_id: str | None = ...,
        entry_type: DirEntryType | str | None = ...,
        target_zone_id: str | None = ...,
        ttl_seconds: float | None = ...,
    ) -> None: ...
