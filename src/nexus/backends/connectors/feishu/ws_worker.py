"""Feishu WebSocket (Long Connection) worker for zero-config event ingestion.

Uses ``lark_oapi.ws.Client`` to maintain a persistent outbound connection
to Feishu's event gateway. No public URL, no ngrok, no webhook challenge
needed — works behind NAT and firewalls.

Events received over the WebSocket are translated to Nexus ``FileEvent``
objects via the shared ``translate_feishu_event`` and published to the
EventBus for downstream processing.

Usage::

    worker = FeishuWebSocketWorker(
        app_id="cli_xxx",
        app_secret="secret",
        event_bus=event_bus,
    )
    worker.start()   # Blocking — run in a daemon thread
    worker.stop()    # Graceful shutdown
"""

import json
import logging
import threading
from typing import Any

from nexus.backends.connectors.feishu.events import translate_feishu_event

logger = logging.getLogger(__name__)


# Module-level callbacks for cache invalidation
_cache_invalidators: list[Any] = []


def register_cache_invalidator(callback: Any) -> None:
    """Register a callback for cache invalidation on inbound WS events."""
    _cache_invalidators.append(callback)
    logger.info("Feishu WS worker cache invalidator registered")


class FeishuWebSocketWorker:
    """Persistent WebSocket connection to Feishu event gateway.

    Translates inbound Feishu events to FileEvents and publishes them
    to the Nexus EventBus. Runs in a daemon thread to avoid blocking
    the async server loop.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        event_bus: Any = None,
        *,
        debug_echo: bool = False,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._event_bus = event_bus
        self._debug_echo = debug_echo
        self._thread: threading.Thread | None = None
        self._ws_client: Any = None
        self._lark_client: Any = None
        self._stopped = threading.Event()

    def _on_message_receive(self, data: Any) -> None:
        """Handle im.message.receive_v1 events."""
        try:
            event = data.event
            if not event or not event.message:
                return

            msg = event.message
            event_dict = {
                "message": {
                    "chat_id": msg.chat_id or "unknown",
                    "chat_type": msg.chat_type or "group",
                    "message_id": msg.message_id,
                    "message_type": msg.message_type,
                    "content": msg.content or "",
                }
            }

            file_event = translate_feishu_event("im.message.receive_v1", event_dict)
            if file_event:
                self._publish_and_invalidate(file_event)
                if self._debug_echo:
                    self._echo_reply(msg.chat_id, file_event.path)

        except Exception as e:
            logger.error("Error handling message event: %s", e, exc_info=True)

    def _on_bot_added(self, data: Any) -> None:
        """Handle im.chat.member.bot.added_v1 events."""
        try:
            event = data.event
            if not event:
                return

            chat_id = getattr(event, "chat_id", None) or "unknown"
            event_dict = {"chat_id": chat_id}

            file_event = translate_feishu_event("im.chat.member.bot.added_v1", event_dict)
            if file_event:
                self._publish_and_invalidate(file_event)

        except Exception as e:
            logger.error("Error handling bot-added event: %s", e, exc_info=True)

    def _on_bot_deleted(self, data: Any) -> None:
        """Handle im.chat.member.bot.deleted_v1 events."""
        try:
            event = data.event
            if not event:
                return

            chat_id = getattr(event, "chat_id", None) or "unknown"
            event_dict = {"chat_id": chat_id}

            file_event = translate_feishu_event("im.chat.member.bot.deleted_v1", event_dict)
            if file_event:
                self._publish_and_invalidate(file_event)

        except Exception as e:
            logger.error("Error handling bot-deleted event: %s", e, exc_info=True)

    def _echo_reply(self, chat_id: str, vfs_path: str) -> None:
        """Send a debug echo reply with the VFS path the event was mapped to."""
        if not self._lark_client:
            return
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            body = (
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": f"[nexus-echo] {vfs_path}"}))
                .build()
            )
            req = (
                CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
            )
            resp = self._lark_client.im.v1.message.create(req)
            if not resp.success():
                logger.warning("Echo reply failed: %s %s", resp.code, resp.msg)
        except Exception as e:
            logger.warning("Echo reply error: %s", e)

    def _publish_and_invalidate(self, file_event: Any) -> None:
        """Publish a FileEvent to EventBus and run cache invalidators."""
        # Publish to EventBus
        if self._event_bus:
            try:
                # EventBus.publish() may be async — use fire-and-forget
                import asyncio

                try:
                    loop = asyncio.get_running_loop()
                    loop.call_soon_threadsafe(
                        asyncio.ensure_future, self._event_bus.publish(file_event)
                    )
                except RuntimeError:
                    # No running loop — try creating one
                    asyncio.run(self._event_bus.publish(file_event))

                logger.info(
                    "WS published FileEvent: type=%s path=%s",
                    file_event.type,
                    file_event.path,
                )
            except Exception as e:
                logger.error("WS failed to publish event: %s", e)

        # Cache invalidation callbacks
        for invalidator in _cache_invalidators:
            try:
                invalidator(file_event)
            except Exception as e:
                logger.error("WS cache invalidation failed: %s", e)

    def _build_event_handler(self) -> Any:
        """Build the lark_oapi EventDispatcherHandler with registered callbacks."""
        import lark_oapi as lark

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .register_p2_im_chat_member_bot_added_v1(self._on_bot_added)
            .register_p2_im_chat_member_bot_deleted_v1(self._on_bot_deleted)
            .build()
        )
        return handler

    def _run(self) -> None:
        """Thread target: connect and block on the WebSocket."""
        try:
            import lark_oapi as lark

            if self._debug_echo:
                self._lark_client = (
                    lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
                )

            handler = self._build_event_handler()

            self._ws_client = lark.ws.Client(
                app_id=self.app_id,
                app_secret=self.app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.INFO,
                auto_reconnect=True,
            )

            logger.info(
                "Feishu WS worker starting (app_id=%s, auto_reconnect=True)",
                self.app_id,
            )
            self._ws_client.start()  # Blocks until disconnected

        except Exception as e:
            if not self._stopped.is_set():
                logger.error("Feishu WS worker crashed: %s", e, exc_info=True)

    def start(self) -> None:
        """Start the WebSocket worker in a daemon thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Feishu WS worker already running")
            return

        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="feishu-ws-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("Feishu WS worker thread started")

    def stop(self) -> None:
        """Stop the WebSocket worker gracefully."""
        self._stopped.set()

        if self._ws_client:
            try:
                # lark_oapi ws.Client doesn't expose a stop() method,
                # but as a daemon thread it will be cleaned up on process exit.
                # Set the flag so the error handler knows it's intentional.
                logger.info("Feishu WS worker stop requested")
            except Exception as e:
                logger.warning("Error stopping WS client: %s", e)

        self._ws_client = None

    @property
    def is_alive(self) -> bool:
        """Check if the worker thread is still running."""
        return self._thread is not None and self._thread.is_alive()
