"""Preconfigured bearer authentication for Streamable HTTP, without an OAuth issuer."""
import hmac
import os

from starlette.responses import JSONResponse
from scweet_mcp.store import StoreError


class BearerGate:
    def __init__(self, app, token):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            supplied = headers.get(b"authorization", b"")
            if not hmac.compare_digest(supplied, b"Bearer " + self.token):
                response = JSONResponse({"error": "unauthorized"}, status_code=401,
                                        headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def http_app(server, config):
    token = os.environ.get(config.mcp_api_token_env, "")
    if len(token) < 32:
        raise StoreError("CONFIG_ERROR: Set the MCP API token environment reference to a random secret of at least 32 characters.")
    return BearerGate(server.streamable_http_app(), token)
