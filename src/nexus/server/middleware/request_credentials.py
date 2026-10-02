"""Bind HTTP caller credentials for downstream RPCs for exactly one request."""

from starlette.types import ASGIApp, Receive, Scope, Send

from nexus.lib.request_credentials import api_key_from_authorization, request_api_key


class RequestCredentialsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        values = [
            value for key, value in scope.get("headers", ()) if key.lower() == b"authorization"
        ]
        credential = (
            api_key_from_authorization(values[0].decode("latin-1")) if len(values) == 1 else None
        )
        if len(values) > 1:
            credential = ""
        token = request_api_key.set(credential)
        try:
            await self.app(scope, receive, send)
        finally:
            request_api_key.reset(token)
