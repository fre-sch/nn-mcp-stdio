"""The Context injected into handlers: the live connection, scoped to a call.

A handler that declares a parameter annotated `Context` (any name) is handed one
by the server. Through it the handler talks back to the client mid-call:
logging (`notifications/message`) and progress (`notifications/progress`). Those
notifications are enqueued on the server's single outbound queue -- no second
writer. That queue belongs to the server's event loop, so a `Context` refuses to
emit from any other loop or thread rather than race.

Blocking work goes into a thread through `await context.to_thread(func, ...)`,
which hands `func` a `ContextThreadSafe`: the same logging and progress as plain
methods safe to call from that thread, plus the request's cancellation.

The server->client *request* back-channel (sampling, elicitation, roots) is a
later phase.
"""

import asyncio
import threading
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
        self._loop = asyncio.get_running_loop()

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
        await self._emit(self._log_notification(level, data, logger))

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
        notification = self._progress_notification(progress, total, message)
        if notification is not None:
            await self._emit(notification)

    async def to_thread(
        self, function: typing.Callable, /, *args, **kwargs
    ) -> typing.Any:
        """Run blocking `function(*args, **kwargs)` in a worker thread.

        The parameter of `function` annotated `ContextThreadSafe`, whatever its
        name, receives one for this call. When the request is cancelled, this
        await ends at once; the thread runs on until it checks
        `cancelled` or calls `raise_if_cancelled()`.
        """
        cancelled = threading.Event()
        name = annotated_parameter(function, ContextThreadSafe)
        if name is not None:
            kwargs[name] = ContextThreadSafe(self, cancelled)
        try:
            result = await asyncio.to_thread(function, *args, **kwargs)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        # The thread's messages are callbacks already queued on the loop, but
        # the result can arrive without this task yielding (the future may be
        # done by the time it is awaited). Yield once so they land ahead of
        # the handler's reply.
        await asyncio.sleep(0)
        return result

    def _log_notification(self, level, data, logger):
        return self._notification(
            mcp_logging.LOGGING_MESSAGE,
            mcp_logging.LoggingMessageNotificationParams(
                level=level, data=data, logger=logger
            ),
        )

    def _progress_notification(self, progress, total, message):
        if self._progress_token is None:
            return None
        return self._notification(
            common.PROGRESS,
            common.ProgressNotificationParams(
                progress_token=self._progress_token,
                progress=progress,
                total=total,
                message=message,
            ),
        )

    def _notification(self, method, params):
        return jsonrpc.Notification(
            method=method, params=params, jsonrpc=jsonrpc.VERSION
        )

    async def _emit(self, notification):
        self._require_own_loop()
        await self._outbox.put(self._encode(notification))

    def _emit_threadsafe(self, notification):
        # The put itself runs on the server's loop; callbacks run in order, so
        # a thread's messages precede the reply to its handler.
        line = self._encode(notification)
        self._loop.call_soon_threadsafe(self._outbox.put_nowait, line)

    def _require_own_loop(self):
        # The outbound queue is not thread-safe: a put from another loop or
        # thread races the writer, silently in normal mode.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is self._loop:
            return
        raise RuntimeError(
            "Context used outside the event loop it belongs to -- from another "
            "thread or event loop; run blocking work with "
            "`await context.to_thread(function, ...)` and use the "
            "ContextThreadSafe it passes"
        )


class ContextThreadSafe:
    """The `Context` of a call, for a function run by `context.to_thread`.

    Every method is plain and safe to call from the worker thread: each hands
    its finished message to the server's event loop. A thread cannot be stopped
    from outside, so a cancelled request reaches it only where it checks
    `cancelled` or calls `raise_if_cancelled()`.
    """

    def __init__(self, context: Context, cancelled: threading.Event) -> None:
        self._context = context
        self._cancelled = cancelled

    @property
    def request_id(self) -> int | str | None:
        """The id of the in-flight request."""
        return self._context.request_id

    @property
    def client(self) -> Implementation | None:
        """The client's `Implementation`, as captured at `initialize`."""
        return self._context.client

    @property
    def cancelled(self) -> bool:
        """Whether the client cancelled the request; its result is unused."""
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise `asyncio.CancelledError` once the request is cancelled."""
        if self._cancelled.is_set():
            raise asyncio.CancelledError()

    def log(
        self,
        level: mcp_logging.LoggingLevel,
        data: typing.Any,
        *,
        logger: str | None = None,
    ) -> None:
        """Send a `notifications/message` at `level` (RFC 5424)."""
        self._context._emit_threadsafe(
            self._context._log_notification(level, data, logger)
        )

    def debug(self, data: typing.Any, *, logger: str | None = None) -> None:
        self.log("debug", data, logger=logger)

    def info(self, data: typing.Any, *, logger: str | None = None) -> None:
        self.log("info", data, logger=logger)

    def warning(self, data: typing.Any, *, logger: str | None = None) -> None:
        self.log("warning", data, logger=logger)

    def error(self, data: typing.Any, *, logger: str | None = None) -> None:
        self.log("error", data, logger=logger)

    def report_progress(
        self,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        """Send a `notifications/progress` update; a no-op without a token."""
        notification = self._context._progress_notification(
            progress, total, message
        )
        if notification is not None:
            self._context._emit_threadsafe(notification)


def context_parameter(handler: typing.Callable) -> str | None:
    """The name of `handler`'s `Context`-annotated parameter, or `None`.

    Any parameter name works; detection is by the annotation being `Context`.
    """
    return annotated_parameter(handler, Context)


def annotated_parameter(function: typing.Callable, cls: type) -> str | None:
    """The name of `function`'s parameter annotated `cls`, or `None`."""
    try:
        hints = typing.get_type_hints(function, include_extras=True)
    except Exception:
        hints = getattr(function, "__annotations__", {})
    for name, hint in hints.items():
        if name != "return" and _strip_annotated(hint) is cls:
            return name
    return None


def _strip_annotated(hint):
    if typing.get_origin(hint) is typing.Annotated:
        return typing.get_args(hint)[0]
    return hint
