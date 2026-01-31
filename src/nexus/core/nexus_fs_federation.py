"""Federation mixin for P2P multi-box support.

This mixin adds transparent federation routing to NexusFilesystem,
enabling seamless access to files on remote boxes within the same federation.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_transport import NexusRPCTransport, RPCError, TransportError

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


class NexusFSFederationMixin:
    """Mixin that adds P2P federation support to NexusFilesystem.

    When a path belongs to a remote box, requests are transparently
    forwarded via RPC transport. Local paths are handled normally.

    Usage:
        class NexusFS(
            NexusFSFederationMixin,  # Must be first for method interception
            NexusFSCoreMixin,
            ...
        ):
            pass

        nx = NexusFS(
            backend=local_backend,
            federation_enabled=True,
            federation_token="federation-secret-token",
            local_box_id="box-a",
        )

        # Transparent access - works for both local and remote
        content = nx.read("/mnt/remote-box/file.txt")  # Forwarded to remote
        content = nx.read("/workspace/local.txt")       # Handled locally
    """

    # These will be set by __init__
    _federation_enabled: bool
    _federation_token: str | None
    _local_box_id: str | None
    _transports: dict[str, NexusRPCTransport]

    def _init_federation(
        self,
        federation_enabled: bool = False,
        federation_token: str | None = None,
        local_box_id: str | None = None,
    ) -> None:
        """Initialize federation support.

        Called from NexusFS.__init__ to set up federation state.

        Args:
            federation_enabled: Enable federation routing (default: False)
            federation_token: Token for authenticating with remote boxes
            local_box_id: This box's unique identifier
        """
        self._federation_enabled = federation_enabled
        self._federation_token = federation_token
        self._local_box_id = local_box_id
        self._transports: dict[str, NexusRPCTransport] = {}

        if federation_enabled:
            logger.info(
                f"Federation enabled: local_box_id={local_box_id}, "
                f"token={'set' if federation_token else 'not set'}"
            )

    def _is_remote_path(self, path: str) -> tuple[bool, dict[str, Any] | None]:
        """Check if path belongs to a remote box.

        Looks up the mount configuration to determine if the path's mount
        is owned by a different box.

        Args:
            path: Virtual path to check

        Returns:
            Tuple of (is_remote, remote_info)
            remote_info contains: box_id, endpoint (if remote)
        """
        if not self._federation_enabled:
            return False, None

        # Get the mount for this path
        try:
            route = self.router.route(path, check_write=False)  # type: ignore[attr-defined]  # allowed
            mount = self.router.get_mount(route.mount_point)  # type: ignore[attr-defined]  # allowed

            if mount is None:
                return False, None

            # Check if mount has remote box info in backend_config
            # This is set when a remote mount is synced via Dragonfly
            backend_config = getattr(mount, "backend_config", None)
            if backend_config is None:
                return False, None

            # Parse backend_config if it's a string (JSON)
            if isinstance(backend_config, str):
                import json

                try:
                    backend_config = json.loads(backend_config)
                except json.JSONDecodeError:
                    return False, None

            box_id = backend_config.get("box_id")
            endpoint = backend_config.get("endpoint")

            # Check if this mount belongs to a different box
            if box_id and box_id != self._local_box_id and endpoint:
                return True, {"box_id": box_id, "endpoint": endpoint}

            return False, None

        except Exception as e:
            logger.debug(f"Error checking remote path {path}: {e}")
            return False, None

    def _get_transport(self, endpoint: str) -> NexusRPCTransport:
        """Get or create transport for remote endpoint.

        Maintains a pool of transports for connection reuse.

        Args:
            endpoint: Remote box endpoint URL

        Returns:
            NexusRPCTransport instance
        """
        if endpoint not in self._transports:
            logger.debug(f"Creating transport for endpoint: {endpoint}")
            self._transports[endpoint] = NexusRPCTransport(
                endpoint=endpoint,
                auth_token=self._federation_token,
            )
        return self._transports[endpoint]

    def _forward_read(
        self,
        path: str,
        remote_info: dict[str, Any],
        return_metadata: bool = False,
        parsed: bool = False,
    ) -> bytes | dict[str, Any]:
        """Forward read request to remote box.

        Args:
            path: Virtual path to read
            remote_info: Remote box info (endpoint, box_id)
            return_metadata: Whether to return metadata with content
            parsed: Whether to return parsed content

        Returns:
            File content (bytes) or dict with content and metadata
        """
        transport = self._get_transport(remote_info["endpoint"])

        try:
            result = transport.call(
                "read",
                {
                    "path": path,
                    "return_metadata": return_metadata,
                    "parsed": parsed,
                },
            )

            if return_metadata:
                # Result is already a dict with content and metadata
                if "content" in result and isinstance(result["content"], str):
                    result["content"] = base64.b64decode(result["content"])
                return dict(result)
            else:
                # Result is base64-encoded content
                if isinstance(result, dict) and "content" in result:
                    return base64.b64decode(result["content"])
                elif isinstance(result, str):
                    return base64.b64decode(result)
                else:
                    return bytes(result)

        except RPCError as e:
            # Convert RPC errors to appropriate Nexus exceptions
            from nexus.core.exceptions import (
                NexusFileNotFoundError,
                NexusPermissionError,
            )
            from nexus.server.protocol import RPCErrorCode

            if e.code == RPCErrorCode.FILE_NOT_FOUND.value:
                raise NexusFileNotFoundError(path) from e
            elif e.code == RPCErrorCode.PERMISSION_ERROR.value:
                raise NexusPermissionError(f"Access denied: {path}") from e
            else:
                raise

        except TransportError as e:
            logger.error(f"Federation transport error for {path}: {e}")
            raise

    def _forward_write(
        self,
        path: str,
        content: bytes,
        remote_info: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Forward write request to remote box."""
        transport = self._get_transport(remote_info["endpoint"])

        result = transport.call(
            "write",
            {
                "path": path,
                "content": base64.b64encode(content).decode(),
                **kwargs,
            },
        )
        return dict(result)

    def _forward_list(
        self,
        path: str,
        remote_info: dict[str, Any],
        **kwargs: Any,
    ) -> list[Any]:
        """Forward list request to remote box."""
        transport = self._get_transport(remote_info["endpoint"])

        result = transport.call(
            "list",
            {"path": path, **kwargs},
        )
        return list(result)

    def _forward_exists(
        self,
        path: str,
        remote_info: dict[str, Any],
    ) -> bool:
        """Forward exists request to remote box."""
        transport = self._get_transport(remote_info["endpoint"])

        result = transport.call("exists", {"path": path})
        return bool(result.get("exists", False))

    def _forward_delete(
        self,
        path: str,
        remote_info: dict[str, Any],
    ) -> dict[str, Any]:
        """Forward delete request to remote box."""
        transport = self._get_transport(remote_info["endpoint"])

        return dict(transport.call("delete", {"path": path}))

    def _forward_mkdir(
        self,
        path: str,
        remote_info: dict[str, Any],
        parents: bool = False,
        exist_ok: bool = False,
    ) -> dict[str, Any]:
        """Forward mkdir request to remote box."""
        transport = self._get_transport(remote_info["endpoint"])

        return dict(
            transport.call("mkdir", {"path": path, "parents": parents, "exist_ok": exist_ok})
        )

    # === Method Overrides ===
    # These intercept calls and forward to remote if needed

    def read(
        self,
        path: str,
        context: OperationContext | None = None,
        return_metadata: bool = False,
        parsed: bool = False,
    ) -> bytes | dict[str, Any]:
        """Read file - forwards to remote box if path is remote."""
        is_remote, remote_info = self._is_remote_path(path)

        if is_remote and remote_info:
            logger.debug(f"Forwarding read to remote box: {remote_info['box_id']}")
            return self._forward_read(path, remote_info, return_metadata, parsed)

        # Local path - delegate to CoreMixin
        return super().read(path, context, return_metadata, parsed)  # type: ignore[misc, no-any-return]  # allowed

    def write(
        self,
        path: str,
        content: bytes | str,
        context: OperationContext | None = None,
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Write file - forwards to remote box if path is remote."""
        is_remote, remote_info = self._is_remote_path(path)

        # Convert str to bytes for forwarding
        content_bytes = content.encode() if isinstance(content, str) else content

        if is_remote and remote_info:
            logger.debug(f"Forwarding write to remote box: {remote_info['box_id']}")
            return self._forward_write(
                path,
                content_bytes,
                remote_info,
                if_match=if_match,
                if_none_match=if_none_match,
                force=force,
            )

        return super().write(  # type: ignore[misc, no-any-return]  # allowed
            path,
            content,
            context,
            if_match=if_match,
            if_none_match=if_none_match,
            force=force,
        )

    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
        show_parsed: bool = True,
        context: OperationContext | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> list[Any]:
        """List directory - forwards to remote box if path is remote."""
        is_remote, remote_info = self._is_remote_path(path)

        if is_remote and remote_info:
            logger.debug(f"Forwarding list to remote box: {remote_info['box_id']}")
            return self._forward_list(
                path,
                remote_info,
                recursive=recursive,
                details=details,
                prefix=prefix,
                show_parsed=show_parsed,
                limit=limit,
                cursor=cursor,
            )

        return super().list(  # type: ignore[misc, no-any-return]  # allowed
            path,
            recursive=recursive,
            details=details,
            prefix=prefix,
            show_parsed=show_parsed,
            context=context,
            limit=limit,
            cursor=cursor,
        )

    def exists(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> bool:
        """Check existence - forwards to remote box if path is remote."""
        is_remote, remote_info = self._is_remote_path(path)

        if is_remote and remote_info:
            logger.debug(f"Forwarding exists to remote box: {remote_info['box_id']}")
            return self._forward_exists(path, remote_info)

        return super().exists(path, context)  # type: ignore[misc, no-any-return]  # allowed

    def delete(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> None:
        """Delete file - forwards to remote box if path is remote."""
        is_remote, remote_info = self._is_remote_path(path)

        if is_remote and remote_info:
            logger.debug(f"Forwarding delete to remote box: {remote_info['box_id']}")
            self._forward_delete(path, remote_info)
            return

        super().delete(path, context)  # type: ignore[misc]  # allowed

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        """Create directory - forwards to remote box if path is remote."""
        is_remote, remote_info = self._is_remote_path(path)

        if is_remote and remote_info:
            logger.debug(f"Forwarding mkdir to remote box: {remote_info['box_id']}")
            self._forward_mkdir(path, remote_info, parents, exist_ok)
            return

        super().mkdir(path, parents, exist_ok, context)  # type: ignore[misc]  # allowed

    def close_federation(self) -> None:
        """Close all federation transports.

        Should be called when shutting down to release resources.
        """
        for endpoint, transport in self._transports.items():
            logger.debug(f"Closing transport for: {endpoint}")
            transport.close()
        self._transports.clear()
