"""RPC server for Nexus filesystem.

This module implements an HTTP server that exposes all NexusFileSystem
operations through a clean JSON-RPC API. This allows remote clients
(including FUSE mounts) to access Nexus over the network.

The server maps each NexusFilesystem method to an RPC endpoint:
- POST /api/nfs/read
- POST /api/nfs/write
- POST /api/nfs/list
- POST /api/nfs/glob
- etc.

Authentication is done via simple API key in the Authorization header.
"""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

from nexus import NexusFilesystem
from nexus.core.exceptions import (
    InvalidPathError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)
from nexus.server.protocol import (
    RPCErrorCode,
    RPCRequest,
    RPCResponse,
    decode_rpc_message,
    encode_rpc_message,
    parse_method_params,
)

logger = logging.getLogger(__name__)


class RPCRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Nexus RPC API.

    Implements JSON-RPC 2.0 protocol for all NexusFilesystem operations.
    """

    # Class-level attributes set by server
    nexus_fs: NexusFilesystem
    api_key: str | None = None

    def log_message(self, format: str, *args: Any) -> None:
        """Override to use Python logging instead of stderr."""
        logger.info(f"{self.address_string()} - {format % args}")

    def _set_cors_headers(self) -> None:
        """Set CORS headers to allow requests from frontend."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self) -> None:
        """Handle OPTIONS requests (CORS preflight)."""
        self.send_response(200)
        self._set_cors_headers()
        self.end_headers()

    def do_POST(self) -> None:
        """Handle POST requests (all RPC methods)."""
        try:
            # Parse URL
            parsed = urlparse(self.path)
            path_parts = parsed.path.strip("/").split("/")

            # Check if this is an RPC endpoint
            # Expected: /api/nfs/{method}
            if len(path_parts) != 3 or path_parts[0] != "api" or path_parts[1] != "nfs":
                self._send_error_response(
                    None, RPCErrorCode.INVALID_REQUEST, "Invalid endpoint path"
                )
                return

            method_name = path_parts[2]

            # Validate authentication
            if not self._validate_auth():
                self._send_error_response(
                    None, RPCErrorCode.ACCESS_DENIED, "Invalid or missing API key"
                )
                return

            # Read request body
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                self._send_error_response(None, RPCErrorCode.INVALID_REQUEST, "Empty request body")
                return

            body = self.rfile.read(content_length)

            # Parse JSON-RPC request
            try:
                request_dict = decode_rpc_message(body)
                request = RPCRequest.from_dict(request_dict)
            except Exception as e:
                self._send_error_response(
                    None, RPCErrorCode.PARSE_ERROR, f"Failed to parse request: {e}"
                )
                return

            # Validate method matches URL
            if request.method and request.method != method_name:
                self._send_error_response(
                    request.id,
                    RPCErrorCode.INVALID_REQUEST,
                    f"Method mismatch: URL={method_name}, body={request.method}",
                )
                return

            # Set method from URL if not in body
            if not request.method:
                request.method = method_name

            # Handle RPC call
            self._handle_rpc_call(request)

        except Exception as e:
            logger.exception("Error handling POST request")
            self._send_error_response(None, RPCErrorCode.INTERNAL_ERROR, str(e))

    def do_GET(self) -> None:
        """Handle GET requests (health check, status)."""
        try:
            parsed = urlparse(self.path)

            # Health check endpoint
            if parsed.path == "/health":
                self._send_json_response(200, {"status": "healthy", "service": "nexus-rpc"})
                return

            # Status endpoint
            if parsed.path == "/api/nfs/status":
                self._send_json_response(
                    200,
                    {
                        "status": "running",
                        "service": "nexus-rpc",
                        "version": "1.0",
                        "methods": [
                            "read",
                            "write",
                            "delete",
                            "exists",
                            "list",
                            "glob",
                            "grep",
                            "mkdir",
                            "rmdir",
                            "is_directory",
                        ],
                    },
                )
                return

            self.send_response(404)
            self.end_headers()

        except Exception:
            logger.exception("Error handling GET request")
            self.send_response(500)
            self.end_headers()

    def _validate_auth(self) -> bool:
        """Validate API key authentication.

        Returns:
            True if authentication is valid or not required
        """
        # If no API key is configured, allow all requests
        if not self.api_key:
            return True

        # Check Authorization header
        auth_header = self.headers.get("Authorization")
        if not auth_header:
            return False

        # Expected format: "Bearer <api_key>"
        if not auth_header.startswith("Bearer "):
            return False

        token = auth_header[7:]  # Remove "Bearer " prefix
        return bool(token == self.api_key)

    def _handle_rpc_call(self, request: RPCRequest) -> None:
        """Handle RPC method call.

        Args:
            request: Parsed RPC request
        """
        method = request.method

        try:
            # Parse and validate parameters
            params = parse_method_params(method, request.params)

            # Dispatch to appropriate method
            result = self._dispatch_method(method, params)

            # Send success response
            response = RPCResponse.success(request.id, result)
            self._send_rpc_response(response)

        except ValueError as e:
            # Invalid parameters
            self._send_error_response(
                request.id, RPCErrorCode.INVALID_PARAMS, f"Invalid parameters: {e}"
            )
        except NexusFileNotFoundError as e:
            self._send_error_response(
                request.id, RPCErrorCode.FILE_NOT_FOUND, str(e), data={"path": str(e)}
            )
        except FileExistsError as e:
            self._send_error_response(request.id, RPCErrorCode.FILE_EXISTS, str(e))
        except InvalidPathError as e:
            self._send_error_response(request.id, RPCErrorCode.INVALID_PATH, str(e))
        except NexusPermissionError as e:
            self._send_error_response(request.id, RPCErrorCode.PERMISSION_ERROR, str(e))
        except ValidationError as e:
            self._send_error_response(request.id, RPCErrorCode.VALIDATION_ERROR, str(e))
        except NexusError as e:
            self._send_error_response(request.id, RPCErrorCode.INTERNAL_ERROR, f"Nexus error: {e}")
        except Exception as e:
            logger.exception(f"Error executing method {method}")
            self._send_error_response(
                request.id, RPCErrorCode.INTERNAL_ERROR, f"Internal error: {e}"
            )

    def _dispatch_method(self, method: str, params: Any) -> Any:
        """Dispatch RPC method to NexusFilesystem.

        Args:
            method: Method name
            params: Parsed parameters

        Returns:
            Method result
        """
        # Core file operations
        if method == "read":
            return self.nexus_fs.read(params.path)

        elif method == "write":
            self.nexus_fs.write(params.path, params.content)
            return {"success": True}

        elif method == "delete":
            self.nexus_fs.delete(params.path)
            return {"success": True}

        elif method == "exists":
            return {"exists": self.nexus_fs.exists(params.path)}

        # Discovery operations
        elif method == "list":
            files = self.nexus_fs.list(
                params.path,
                recursive=params.recursive,
                details=params.details,
                prefix=params.prefix,
            )
            # Debug: Check what we got
            logger.info(f"List returned {len(files)} items, type={type(files)}")
            if files:
                logger.info(f"First item type: {type(files[0])}, value: {files[0]!r}")

            # Convert to serializable format (handle dataclass objects)
            serializable_files = []
            for file in files:
                if isinstance(file, (dict, str)):
                    serializable_files.append(file)
                else:
                    # Convert dataclass/object to dict
                    logger.warning(f"Found non-serializable object: {type(file)}")
                    if hasattr(file, "__dict__"):
                        serializable_files.append(
                            {
                                k: v
                                for k, v in file.__dict__.items()
                                if not k.startswith("_") and not callable(v)
                            }
                        )
                    else:
                        serializable_files.append(str(file))
            return {"files": serializable_files}

        elif method == "glob":
            matches = self.nexus_fs.glob(params.pattern, params.path)
            return {"matches": matches}

        elif method == "grep":
            results = self.nexus_fs.grep(
                params.pattern,
                path=params.path,
                file_pattern=params.file_pattern,
                ignore_case=params.ignore_case,
                max_results=params.max_results,
            )
            # Convert to serializable format
            serializable_results = []
            for result in results:
                if isinstance(result, dict):
                    serializable_results.append(result)
                elif hasattr(result, "__dict__"):
                    serializable_results.append(
                        {
                            k: v
                            for k, v in result.__dict__.items()
                            if not k.startswith("_") and not callable(v)
                        }
                    )
                else:
                    serializable_results.append(str(result))
            return {"results": serializable_results}

        # Directory operations
        elif method == "mkdir":
            self.nexus_fs.mkdir(params.path, parents=params.parents, exist_ok=params.exist_ok)
            return {"success": True}

        elif method == "rmdir":
            self.nexus_fs.rmdir(params.path, recursive=params.recursive)
            return {"success": True}

        elif method == "is_directory":
            return {"is_directory": self.nexus_fs.is_directory(params.path)}

        else:
            raise ValueError(f"Unknown method: {method}")

    def _send_rpc_response(self, response: RPCResponse) -> None:
        """Send RPC response.

        Args:
            response: RPC response object
        """
        response_dict = response.to_dict()
        body = encode_rpc_message(response_dict)

        self.send_response(200)
        self._set_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_response(
        self, request_id: str | int | None, code: RPCErrorCode, message: str, data: Any = None
    ) -> None:
        """Send error response.

        Args:
            request_id: Request ID (if available)
            code: Error code
            message: Error message
            data: Optional error data
        """
        response = RPCResponse.create_error(request_id, code, message, data)
        self._send_rpc_response(response)

    def _send_json_response(self, status_code: int, data: dict[str, Any]) -> None:
        """Send JSON response.

        Args:
            status_code: HTTP status code
            data: Response data
        """
        body = encode_rpc_message(data)

        self.send_response(status_code)
        self._set_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class NexusRPCServer:
    """RPC server for Nexus filesystem.

    Provides JSON-RPC endpoints for all NexusFilesystem operations.
    """

    def __init__(
        self,
        nexus_fs: NexusFilesystem,
        host: str = "0.0.0.0",
        port: int = 8080,
        api_key: str | None = None,
    ):
        """Initialize server.

        Args:
            nexus_fs: Nexus filesystem instance
            host: Server host
            port: Server port
            api_key: Optional API key for authentication (if None, no auth required)
        """
        self.nexus_fs = nexus_fs
        self.host = host
        self.port = port
        self.api_key = api_key

        # Create HTTP server
        self.server = HTTPServer((host, port), RPCRequestHandler)

        # Configure handler
        RPCRequestHandler.nexus_fs = nexus_fs
        RPCRequestHandler.api_key = api_key

    def serve_forever(self) -> None:
        """Start server and handle requests."""
        logger.info(f"Starting Nexus RPC server on {self.host}:{self.port}")
        logger.info(f"Endpoint: http://{self.host}:{self.port}/api/nfs/{{method}}")
        if self.api_key:
            logger.info("Authentication: API key required")
        else:
            logger.info("Authentication: None (open access)")

        try:
            self.server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Server stopped by user")
            self.shutdown()

    def shutdown(self) -> None:
        """Shutdown server gracefully."""
        logger.info("Shutting down server...")
        self.server.shutdown()
        self.server.server_close()
        if hasattr(self.nexus_fs, "close"):
            self.nexus_fs.close()
        logger.info("Server stopped")
