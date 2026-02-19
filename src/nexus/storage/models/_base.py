"""Shared base, mixins, and utilities for SQLAlchemy models.

Issue #1246 Phase 4: Extracted from monolithic models.py.
Issue #1286: Added mixins (TimestampMixin, ZoneIsolationMixin, ResourceConfigMixin),
             uuid_pk() helper, and _get_uuid_server_default.
Issue #2129: Canonical definitions moved to ``nexus.contracts.db_base``.
             This module re-exports everything for backward compatibility.
"""

from nexus.contracts.db_base import Base as Base
from nexus.contracts.db_base import ResourceConfigMixin as ResourceConfigMixin
from nexus.contracts.db_base import TimestampMixin as TimestampMixin
from nexus.contracts.db_base import ZoneIsolationMixin as ZoneIsolationMixin
from nexus.contracts.db_base import _generate_uuid as _generate_uuid
from nexus.contracts.db_base import _get_uuid_server_default as _get_uuid_server_default
from nexus.contracts.db_base import uuid_pk as uuid_pk
