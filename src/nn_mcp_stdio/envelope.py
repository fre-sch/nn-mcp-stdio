"""Inbound JSON-RPC envelope: parse a line, classify it, validate it strictly.

Every inbound line becomes one of the `nn_mcp_types.jsonrpc` envelope
dataclasses, or raises. The message's shape picks the dataclass -- a `method`
with an `id` is a request, a `method` without one a notification, anything else
a response -- and the message is validated against the schema generated from
that dataclass before it is built, as tool arguments are: with `jsonschema`,
never coerced.
"""

import json

from jsonschema.exceptions import best_match
from jsonschema.validators import Draft202012Validator

from nn_mcp_types import jsonrpc
from nn_mcp_types.schema import get_schema
from nn_mcp_types.wire import from_wire

Message = (
    jsonrpc.Request
    | jsonrpc.Notification
    | jsonrpc.Response
    | jsonrpc.ErrorResponse
)

RESPONSE_TYPES = (jsonrpc.Response, jsonrpc.ErrorResponse)

_VALIDATORS = {
    envelope_type: Draft202012Validator(get_schema(envelope_type))
    for envelope_type in (jsonrpc.Request, jsonrpc.Notification)
    + RESPONSE_TYPES
}


class ParseError(Exception):
    """The line is not JSON."""


class InvalidMessage(Exception):
    """The line is JSON, but not a valid MCP JSON-RPC message.

    `envelope_type` is what the message's shape says it tried to be.
    `request_id` is its `id` when that is a valid request id, else `None`.
    """

    def __init__(
        self, reason: str, envelope_type: type, request_id: int | str | None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.envelope_type = envelope_type
        self.request_id = request_id


def parse(line: str) -> Message:
    """Build the envelope dataclass a line holds, or raise."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError as error:
        raise ParseError(str(error)) from error
    if not isinstance(data, dict):
        raise InvalidMessage(
            "a message is a JSON object", jsonrpc.Request, None
        )
    envelope_type = classify(data)
    invalid = best_match(_VALIDATORS[envelope_type].iter_errors(data))
    if invalid is not None:
        raise InvalidMessage(
            invalid.message, envelope_type, request_id_of(data)
        )
    return from_wire(envelope_type, data)


def classify(data: dict) -> type:
    """The envelope dataclass a message's shape says it is."""
    if "method" in data:
        return jsonrpc.Request if "id" in data else jsonrpc.Notification
    return jsonrpc.ErrorResponse if "error" in data else jsonrpc.Response


def request_id_of(data: dict) -> int | str | None:
    """The message's `id`, when it is one a response can echo."""
    request_id = data.get("id")
    if isinstance(request_id, bool):  # JSON `true` is not an integer id
        return None
    if isinstance(request_id, int | str):
        return request_id
    return None
