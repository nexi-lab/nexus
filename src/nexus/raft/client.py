"""gRPC client for Raft consensus nodes.

This module provides async Python clients to communicate with Rust Raft nodes
over gRPC for metadata and lock operations.

Architecture:
    - RaftClientService (client-facing): Used by RemoteNexusFS for Propose/Query
    - RaftService (internal): Used for node-to-node Raft protocol (not exposed here)

For local same-box scenarios, use LocalRaft (PyO3 FFI) instead for better
performance (~5μs vs ~200μs latency).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import grpc
from grpc import aio as grpc_aio

from nexus.core import metadata_pb2

# Import generated proto types
from nexus.raft import commands_pb2, transport_pb2, transport_pb2_grpc

if TYPE_CHECKING:
    from nexus.core.metadata import FileMetadata

logger = logging.getLogger(__name__)


class RaftError(Exception):
    """Base exception for Raft operations."""

    pass


class RaftNotLeaderError(RaftError):
    """Raised when operation requires leader but connected to follower."""

    def __init__(self, message: str, leader_address: str | None = None):
        super().__init__(message)
        self.leader_address = leader_address


@dataclass
class LockResult:
    """Result of a lock acquisition attempt."""

    acquired: bool
    current_holder: str | None = None
    expires_at_ms: int = 0


@dataclass
class LockInfo:
    """Information about a lock."""

    exists: bool
    holder_id: str | None = None
    expires_at_ms: int = 0
    max_holders: int = 0
    current_holders: int = 0


@dataclass
class RaftClientConfig:
    """Configuration for RaftClient."""

    # Connection settings
    timeout_ms: int = 5000
    connect_timeout_ms: int = 3000

    # Retry settings
    max_retries: int = 3
    retry_delay_ms: int = 100

    # Keep-alive settings
    keepalive_time_ms: int = 10000
    keepalive_timeout_ms: int = 5000


class RaftClient:
    """Async gRPC client for Raft cluster (client-facing API).

    This client uses RaftClientService to communicate with Rust Raft nodes for:
    - Metadata operations (put, get, list, delete) via Propose/Query RPCs
    - Lock operations (acquire, release, extend) via Propose RPC

    Primary user: RemoteNexusFS (client-server architecture)

    Example:
        async with RaftClient("localhost:2026") as client:
            # Put metadata (write - goes through Raft)
            await client.put_metadata(file_metadata)

            # Get metadata (read - can be served by any node)
            metadata = await client.get_metadata("/path/to/file")

            # Acquire lock
            result = await client.acquire_lock(
                lock_id="/path/to/file",
                holder_id="agent-123",
                ttl_ms=30000,
            )
            if result.acquired:
                try:
                    # Do work...
                    pass
                finally:
                    await client.release_lock("/path/to/file", "agent-123")
    """

    def __init__(
        self,
        address: str,
        config: RaftClientConfig | None = None,
        zone_id: str | None = None,
    ):
        """Initialize RaftClient.

        Args:
            address: Raft node address (e.g., "localhost:2026" or "10.0.0.2:2026")
            config: Client configuration
            zone_id: Default zone ID for operations
        """
        self.address = address
        self.config = config or RaftClientConfig()
        self.zone_id = zone_id

        self._channel: grpc_aio.Channel | None = None
        self._stub: transport_pb2_grpc.RaftClientServiceStub | None = None

    async def connect(self) -> None:
        """Establish connection to Raft node."""
        if self._channel is not None:
            return

        # Configure channel options
        options = [
            ("grpc.keepalive_time_ms", self.config.keepalive_time_ms),
            ("grpc.keepalive_timeout_ms", self.config.keepalive_timeout_ms),
            ("grpc.keepalive_permit_without_calls", True),
            ("grpc.http2.max_pings_without_data", 0),
        ]

        self._channel = grpc_aio.insecure_channel(self.address, options=options)
        # Use RaftClientService (client-facing API), NOT RaftService (internal)
        self._stub = transport_pb2_grpc.RaftClientServiceStub(self._channel)

        logger.debug(f"Connected to Raft cluster at {self.address}")

    async def close(self) -> None:
        """Close connection to Raft node."""
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None
            logger.debug(f"Disconnected from Raft node at {self.address}")

    async def __aenter__(self) -> RaftClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    def _ensure_connected(self) -> transport_pb2_grpc.RaftClientServiceStub:
        """Ensure client is connected and return stub."""
        if self._stub is None:
            raise RuntimeError("RaftClient not connected. Use 'async with' or call connect()")
        return self._stub

    # =========================================================================
    # Core RPC Methods (Propose for writes, Query for reads)
    # =========================================================================

    async def _propose(
        self,
        command: commands_pb2.RaftCommand,
        request_id: str | None = None,
    ) -> transport_pb2.ProposeResponse:
        """Send a propose request (write operation) to the Raft leader.

        Args:
            command: RaftCommand to propose
            request_id: Optional request ID for idempotency

        Returns:
            ProposeResponse from the server

        Raises:
            RaftNotLeaderError: If this node is not the leader (includes leader_address)
            grpc.RpcError: On connection/protocol errors
        """
        stub = self._ensure_connected()

        request = transport_pb2.ProposeRequest(
            command=command,
            request_id=request_id or "",
        )

        try:
            response = await stub.Propose(
                request,
                timeout=self.config.timeout_ms / 1000,
            )

            # Handle not-leader response
            if not response.success and response.leader_address:
                raise RaftNotLeaderError(
                    f"Not the leader. Redirect to: {response.leader_address}",
                    leader_address=response.leader_address,
                )

            if not response.success:
                raise RaftError(response.error or "Propose failed")

            return response
        except grpc.RpcError as e:
            logger.error(f"Propose RPC failed: {e}")
            raise

    async def _query(
        self,
        query: commands_pb2.RaftQuery,
        read_from_leader: bool = False,
    ) -> transport_pb2.QueryResponse:
        """Send a query request (read operation) to the Raft node.

        Args:
            query: RaftQuery to execute
            read_from_leader: If True, require linearizable read from leader

        Returns:
            QueryResponse from the server
        """
        stub = self._ensure_connected()

        request = transport_pb2.QueryRequest(
            query=query,
            read_from_leader=read_from_leader,
        )

        try:
            response = await stub.Query(
                request,
                timeout=self.config.timeout_ms / 1000,
            )

            # Handle not-leader response for linearizable reads
            if not response.success and response.leader_address:
                raise RaftNotLeaderError(
                    f"Not the leader. Redirect to: {response.leader_address}",
                    leader_address=response.leader_address,
                )

            if not response.success:
                raise RaftError(response.error or "Query failed")

            return response
        except grpc.RpcError as e:
            logger.error(f"Query RPC failed: {e}")
            raise

    async def get_cluster_info(self) -> dict:
        """Get cluster information.

        Returns:
            Dict with node_id, leader_id, term, is_leader, leader_address
        """
        stub = self._ensure_connected()

        request = transport_pb2.GetClusterInfoRequest()

        try:
            response = await stub.GetClusterInfo(
                request,
                timeout=self.config.timeout_ms / 1000,
            )
            return {
                "node_id": response.node_id,
                "leader_id": response.leader_id,
                "term": response.term,
                "is_leader": response.is_leader,
                "leader_address": response.leader_address or None,
            }
        except grpc.RpcError as e:
            logger.error(f"GetClusterInfo RPC failed: {e}")
            raise

    # =========================================================================
    # Metadata Operations (Write via Propose, Read via Query)
    # =========================================================================

    async def put_metadata(
        self,
        metadata: FileMetadata,
        zone_id: str | None = None,
    ) -> bool:
        """Store file metadata in Raft state machine.

        Args:
            metadata: FileMetadata to store
            zone_id: Zone ID (uses default if not specified)

        Returns:
            True if successful
        """
        zone = zone_id or self.zone_id or ""

        # Convert FileMetadata to proto
        proto_metadata = metadata_pb2.FileMetadata(
            path=metadata.path,
            backend_name=metadata.backend_name,
            physical_path=metadata.physical_path or "",
            size=metadata.size,
            etag=metadata.etag or "",
            mime_type=metadata.mime_type or "",
            created_at=metadata.created_at.isoformat() if metadata.created_at else "",
            modified_at=metadata.modified_at.isoformat() if metadata.modified_at else "",
            version=metadata.version,
            zone_id=zone,
            created_by=metadata.created_by or "",
            is_directory=metadata.is_directory,
            owner_id=metadata.owner_id or "",
        )

        # Create PutMetadata command and propose via Raft
        command = commands_pb2.RaftCommand(
            put_metadata=commands_pb2.PutMetadata(metadata=proto_metadata)
        )

        response = await self._propose(command)
        return response.success

    async def get_metadata(
        self,
        path: str,
        zone_id: str | None = None,
        read_from_leader: bool = False,
    ) -> FileMetadata | None:
        """Get file metadata from Raft state machine.

        Args:
            path: File path to get
            zone_id: Zone ID
            read_from_leader: If True, ensure linearizable read from leader

        Returns:
            FileMetadata if found, None otherwise
        """
        zone = zone_id or self.zone_id or ""

        query = commands_pb2.RaftQuery(
            get_metadata=commands_pb2.GetMetadata(path=path, zone_id=zone)
        )

        response = await self._query(query, read_from_leader)

        # Extract result from QueryResponse
        if response.result and response.result.HasField("get_metadata_result"):
            result = response.result.get_metadata_result
            if result.HasField("metadata"):
                return self._proto_to_file_metadata(result.metadata)

        return None

    async def list_metadata(
        self,
        prefix: str = "",
        zone_id: str | None = None,
        recursive: bool = True,
        limit: int = 0,
        read_from_leader: bool = False,
    ) -> list[FileMetadata]:
        """List file metadata from Raft state machine.

        Args:
            prefix: Path prefix to filter by
            zone_id: Zone ID
            recursive: Include nested files
            limit: Max results (0 = no limit)
            read_from_leader: If True, ensure linearizable read

        Returns:
            List of FileMetadata
        """
        zone = zone_id or self.zone_id or ""

        query = commands_pb2.RaftQuery(
            list_metadata=commands_pb2.ListMetadata(
                prefix=prefix,
                zone_id=zone,
                recursive=recursive,
                limit=limit,
                cursor="",
            )
        )

        response = await self._query(query, read_from_leader)

        # Extract results
        results = []
        if response.result and response.result.HasField("list_metadata_result"):
            for proto in response.result.list_metadata_result.items:
                results.append(self._proto_to_file_metadata(proto))

        return results

    async def delete_metadata(
        self,
        path: str,
        zone_id: str | None = None,
    ) -> bool:
        """Delete file metadata from Raft state machine.

        Args:
            path: File path to delete
            zone_id: Zone ID

        Returns:
            True if successful
        """
        zone = zone_id or self.zone_id or ""

        command = commands_pb2.RaftCommand(
            delete_metadata=commands_pb2.DeleteMetadata(path=path, zone_id=zone)
        )

        response = await self._propose(command)
        return response.success

    def _proto_to_file_metadata(self, proto: metadata_pb2.FileMetadata) -> FileMetadata:
        """Convert proto FileMetadata to dataclass."""
        from nexus.core.metadata import FileMetadata as FM

        created_at = None
        modified_at = None
        if proto.created_at:
            try:
                created_at = datetime.fromisoformat(proto.created_at)
            except ValueError:
                pass
        if proto.modified_at:
            try:
                modified_at = datetime.fromisoformat(proto.modified_at)
            except ValueError:
                pass

        return FM(
            path=proto.path,
            backend_name=proto.backend_name,
            physical_path=proto.physical_path or None,
            size=proto.size,
            etag=proto.etag or None,
            mime_type=proto.mime_type or None,
            created_at=created_at,
            modified_at=modified_at,
            version=proto.version,
            zone_id=proto.zone_id or None,
            created_by=proto.created_by or None,
            is_directory=proto.is_directory,
            owner_id=proto.owner_id or None,
        )

    # =========================================================================
    # Lock Operations (via Propose/Query RPCs)
    # =========================================================================

    async def acquire_lock(
        self,
        lock_id: str,
        holder_id: str,
        ttl_ms: int = 30000,
        zone_id: str | None = None,
    ) -> LockResult:
        """Acquire a distributed lock.

        Args:
            lock_id: Unique lock identifier (typically a resource path)
            holder_id: Identifier of the lock holder (e.g., "agent:xxx")
            ttl_ms: Lock TTL in milliseconds (auto-release on expiry)
            zone_id: Zone ID for isolation

        Returns:
            LockResult with acquisition status
        """
        zone = zone_id or self.zone_id or ""

        command = commands_pb2.RaftCommand(
            acquire_lock=commands_pb2.AcquireLock(
                lock_id=lock_id,
                holder_id=holder_id,
                ttl_ms=ttl_ms,
                zone_id=zone,
            )
        )

        response = await self._propose(command)

        # Extract lock result from response
        if response.result and response.result.HasField("lock_result"):
            lr = response.result.lock_result
            return LockResult(
                acquired=lr.acquired,
                current_holder=lr.current_holder or None,
                expires_at_ms=lr.expires_at_ms,
            )

        return LockResult(acquired=response.success)

    async def release_lock(
        self,
        lock_id: str,
        holder_id: str,
        zone_id: str | None = None,
    ) -> bool:
        """Release a distributed lock.

        Args:
            lock_id: Lock identifier
            holder_id: Must match the current holder
            zone_id: Zone ID

        Returns:
            True if successful
        """
        zone = zone_id or self.zone_id or ""

        command = commands_pb2.RaftCommand(
            release_lock=commands_pb2.ReleaseLock(
                lock_id=lock_id,
                holder_id=holder_id,
                zone_id=zone,
            )
        )

        response = await self._propose(command)
        return response.success

    async def extend_lock(
        self,
        lock_id: str,
        holder_id: str,
        ttl_ms: int = 30000,
        zone_id: str | None = None,
    ) -> bool:
        """Extend a lock's TTL (heartbeat).

        Args:
            lock_id: Lock identifier
            holder_id: Must match the current holder
            ttl_ms: New TTL in milliseconds
            zone_id: Zone ID

        Returns:
            True if successful
        """
        zone = zone_id or self.zone_id or ""

        command = commands_pb2.RaftCommand(
            extend_lock=commands_pb2.ExtendLock(
                lock_id=lock_id,
                holder_id=holder_id,
                ttl_ms=ttl_ms,
                zone_id=zone,
            )
        )

        response = await self._propose(command)
        return response.success

    async def get_lock_info(
        self,
        lock_id: str,
        zone_id: str | None = None,
        read_from_leader: bool = False,
    ) -> LockInfo:
        """Get information about a lock.

        Args:
            lock_id: Lock identifier
            zone_id: Zone ID
            read_from_leader: If True, ensure linearizable read

        Returns:
            LockInfo with lock status
        """
        zone = zone_id or self.zone_id or ""

        query = commands_pb2.RaftQuery(
            get_lock_info=commands_pb2.GetLockInfo(
                lock_id=lock_id,
                zone_id=zone,
            )
        )

        response = await self._query(query, read_from_leader)

        # Extract lock info from response
        if response.result and response.result.HasField("lock_info_result"):
            li = response.result.lock_info_result
            return LockInfo(
                exists=li.exists,
                holder_id=li.holder_id or None,
                expires_at_ms=li.expires_at_ms,
                max_holders=li.max_holders,
                current_holders=li.current_holders,
            )

        return LockInfo(exists=False)

    @asynccontextmanager
    async def lock(
        self,
        lock_id: str,
        holder_id: str,
        ttl_ms: int = 30000,
        zone_id: str | None = None,
    ) -> AsyncIterator[LockResult]:
        """Context manager for acquiring and releasing a lock.

        Example:
            async with client.lock("/path/to/file", "agent-123") as result:
                if result.acquired:
                    # Do work while holding lock
                    pass
        """
        result = await self.acquire_lock(lock_id, holder_id, ttl_ms, zone_id)
        try:
            yield result
        finally:
            if result.acquired:
                await self.release_lock(lock_id, holder_id, zone_id)


@dataclass
class RaftClientPool:
    """Pool of RaftClient connections to multiple Raft nodes.

    Provides automatic leader discovery and failover.
    """

    addresses: list[str]
    config: RaftClientConfig = field(default_factory=RaftClientConfig)
    zone_id: str | None = None

    _clients: dict[str, RaftClient] = field(default_factory=dict, init=False)
    _leader_address: str | None = field(default=None, init=False)

    async def get_client(self) -> RaftClient:
        """Get a client, preferring the leader if known."""
        # For now, just return first available client
        # In real impl, would track leader and route accordingly
        address = self._leader_address or self.addresses[0]

        if address not in self._clients:
            client = RaftClient(address, self.config, self.zone_id)
            await client.connect()
            self._clients[address] = client

        return self._clients[address]

    async def close_all(self) -> None:
        """Close all client connections."""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()

    async def __aenter__(self) -> RaftClientPool:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close_all()
