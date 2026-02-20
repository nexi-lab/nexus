"""ReBAC object type mapping — extracted from Backend ABC.

Maps backend paths to ReBAC object types and IDs, decoupling
permission logic from the storage Backend interface.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.backends.backend import Backend

logger = logging.getLogger(__name__)


class ObjectTypeMapper:
    """Maps backend paths to ReBAC object types and IDs.

    Extracted from Backend.get_object_type() / get_object_id() to keep
    the Backend ABC focused on storage operations.
    """

    def get_object_type(self, backend: Backend, backend_path: str) -> str:
        """Get ReBAC object type for a backend path.

        Delegates to backend.get_object_type() which defaults to 'file'.
        Backends like IPCVFSDriver override to return custom types.

        Args:
            backend: The backend instance
            backend_path: Path relative to backend root

        Returns:
            ReBAC object type string (e.g., 'file', 'ipc:agent')
        """
        try:
            return backend.get_object_type(backend_path)
        except Exception as e:
            logger.warning(
                f"[ObjectTypeMapper] get_object_type failed for "
                f"'{backend_path}' on '{backend.name}': {e}, defaulting to 'file'"
            )
            return "file"

    def get_object_id(
        self,
        backend: Backend,
        backend_path: str,
        virtual_path: str,
        object_type: str,
    ) -> str:
        """Get ReBAC object ID for a backend path.

        For file objects, uses the virtual path (mount-aware) to match
        how ReBAC tuples are created. For non-file objects, delegates
        to the backend.

        Args:
            backend: The backend instance
            backend_path: Path relative to backend root
            virtual_path: Full virtual path (mount-aware)
            object_type: The object type (from get_object_type)

        Returns:
            ReBAC object identifier
        """
        if object_type == "file":
            return virtual_path

        try:
            return backend.get_object_id(backend_path)
        except Exception as e:
            logger.warning(
                f"[ObjectTypeMapper] get_object_id failed for "
                f"'{backend_path}' on '{backend.name}': {e}, using virtual_path"
            )
            return virtual_path
