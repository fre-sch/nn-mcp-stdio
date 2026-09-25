"""The Context injected into handlers: the live connection, scoped to a call.

A handler that declares a parameter annotated `Context` (any name) is handed one
by the server. Through it the handler talks back to the client mid-call:
logging (`notifications/message`) and progress (`notifications/progress`). Those
notifications are enqueued on the server's single outbound queue -- no second
writer.

The server->client *request* back-channel (sampling, elicitation, roots) is a
later phase.
"""

import typing

from nn_mcp_types import common, jsonrpc
from nn_mcp_types import logging as mcp_logging
from nn_mcp_types.lifecycle import Implementation


class Context:
    """The live connection for the current call: logging and progress.

    Handlers do not construct this -- the server injects it into any handler
    that annotates a parameter with `Context`.
    """

    def __init__(
        self,
        *,
        request_id: int | str | None,
        client: Implementation | None,
        progress_token: int | str | None,
        outbox: typing.Any,
        encode: typing.Callable,
    ) -> None:
        self._request_id = request_id
        self._client = client
        self._progress_token = progress_token
        self._outbox = outbox
        self._encode = encode

    @property
    def request_id(self) -> int | str | None:
        """The id of the in-flight request (`None` inside a notification)."""
        return self._request_id

    @property
    def client(self) -> Implementation | None:
        """The client's `Implementation`, as captured at `initialize`."""
        return self._client

    async def log(
        self,
        level: mcp_logging.LoggingLevel,
        data: typing.Any,
        *,
        logger: str | None = None,
    ) -> None:
        """Send a `notifications/message` at `level` (RFC 5424)."""
        await self._emit(
            mcp_logging.LOGGING_MESSAGE,
            mcp_logging.LoggingMessageNotificationParams(
                level=level, data=data, logger=logger
            ),
        )

    async def debug(
        self, data: typing.Any, *, logger: str | None = None
    ) -> None:
        await self.log("debug", data, logger=logger)

    async def info(
        self, data: typing.Any, *, logger: str | None = None
    ) -> None:
        await self.log("info", data, logger=logger)

    async def warning(
        self, data: typing.Any, *, logger: str | None = None
    ) -> None:
        await self.log("warning", data, logger=logger)

    async def error(
        self, data: typing.Any, *, logger: str | None = None
    ) -> None:
        await self.log("error", data, logger=logger)

    async def report_progress(
        self,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        """Send a `notifications/progress` update for this call.

        A no-op when the request carried no `progressToken` -- the client did
        not ask for progress, so there is nothing to address.
        """
        if self._progress_token is None:
            return
        await self._emit(
            common.PROGRESS,
            common.ProgressNotificationParams(
                progress_token=self._progress_token,
                progress=progress,
                total=total,
                message=message,
            ),
        )

    async def _emit(self, method, params):
        notification = jsonrpc.Notification(
            method=method, params=params, jsonrpc=jsonrpc.VERSION
        )
        await self._outbox.put(self._encode(notification))


def context_parameter(handler: typing.Callable) -> str | None:
    """The name of `handler`'s `Context`-annotated parameter, or `None`.

    Any parameter name works; detection is by the annotation being `Context`.
    """
    try:
        hints = typing.get_type_hints(handler, include_extras=True)
    except Exception:
        hints = getattr(handler, "__annotations__", {})
    for name, hint in hints.items():
        if name != "return" and _strip_annotated(hint) is Context:
            return name
    return None


def _strip_annotated(hint):
    if typing.get_origin(hint) is typing.Annotated:
        return typing.get_args(hint)[0]
    return hint
