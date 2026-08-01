"""Handler-signalled JSON-RPC errors.

A request handler raises `RequestError` to answer with a specific error code
(e.g. invalid params) instead of the server's default internal error. Kept in
its own module so both the server and the tool layer can raise it without an
import cycle.
"""

from nn_mcp_types import jsonrpc

# MCP defines resource-not-found in the JSON-RPC server-error range (not a
# JSON-RPC standard code), so it lives here rather than in nn_mcp_types.jsonrpc.
RESOURCE_NOT_FOUND = -32002


class RequestError(Exception):
    """A JSON-RPC error a handler chose to raise, carrying its wire code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def invalid_params(message: str) -> RequestError:
    """The arguments were malformed or failed validation (`-32602`)."""
    return RequestError(jsonrpc.INVALID_PARAMS, message)


def resource_not_found(uri: str) -> RequestError:
    """The requested resource URI is not registered (`-32002`)."""
    return RequestError(RESOURCE_NOT_FOUND, f"resource not found: {uri!r}")
