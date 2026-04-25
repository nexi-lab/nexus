"""ReBAC object type mapping — extracted from Backend ABC.

Maps backend paths to ReBAC object types and IDs, decoupling
permission logic from the storage Backend interface.
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.backends.base.backend import Backend

logger = logging.getLogger(__name__)


class ObjectTypeMapper:
    """Maps backend paths to ReBAC object types and IDs.

    Extracted from Backend.get_object_type() / get_object_id() to keep
    the Backend ABC focused on storage operations.
    """

    def get_object_type(self, backend: "Backend", backend_path: str) -> str:
        """Get ReBAC object type for a backend path.

        Delegates to backend.get_object_type() which defaults to 'file'.
        Backends may override to return custom types.

        Args:
            backend: The backend instance
            backend_path: Path relative to backend root

        Returns:
            ReBAC object type string (e.g., 'file', 'ipc:agent')
        """
        try:
            return backend.get_object_type(backend_path)
        except (AttributeError, ValueError, KeyError, NotImplementedError) as e:
            logger.warning(
                f"[ObjectTypeMapper] get_object_type failed for "
                f"'{backend_path}' on '{backend.name}': {e}, defaulting to 'file'"
            )
            return "file"

    def get_object_type_by_name(self, backend_name: str, backend_path: str) -> str:
        """Get ReBAC object type from backend name (no Python backend object).

        Used when only the backend_name string is available (DLC.resolve_path
        now returns strings instead of backend objects).  Defaults to 'file'.
        """
        _ = backend_name, backend_path  # reserved for future type inference
        return "file"

    def get_object_id_by_name(
        self,
        backend_name: str,
        backend_path: str,
        virtual_path: str,
        object_type: str,
    ) -> str:
        """Get ReBAC object ID from backend name (no Python backend object).

        For file objects, uses the virtual path.  Non-file objects also
        default to virtual_path since the Python backend is not available.
        """
        _ = backend_name, backend_path, object_type  # reserved for future inference
        return virtual_path

    def get_object_id(
        self,
        backend: "Backend",
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
        except (AttributeError, ValueError, KeyError, NotImplementedError) as e:
            logger.warning(
                f"[ObjectTypeMapper] get_object_id failed for "
                f"'{backend_path}' on '{backend.name}': {e}, using virtual_path"
            )
            return virtual_path
