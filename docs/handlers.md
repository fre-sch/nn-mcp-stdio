# Other handlers

Tools and resources are the two typed building blocks. Beneath them, the server
dispatches every JSON-RPC message by method name, and you can register a handler
for any method or notification directly. Use these for protocol methods the
framework does not model for you, or to override a built-in.

Both decorators take a plain coroutine of `params` (the request's `params`
object, or `None`). A `Context` parameter is injected here too -- see
[Context](context.md).

## Requests

`@server.request(method)` registers a coroutine `handler(params) -> result`.
The returned value becomes the JSON-RPC response `result`:

```python
@server.request("ping")
async def ping(params):
    return {}
```

To answer with a JSON-RPC error, raise one from `nn_mcp_stdio.errors`; any other
exception becomes an `INTERNAL_ERROR` response.

The framework already registers `initialize`, `logging/setLevel`, `tools/list`,
`tools/call`, `resources/list`, `resources/templates/list`, and `resources/read`.
Registering the same method replaces the built-in -- do so only when you mean
to.

`ping` is answered by the server itself, straight from the loop that reads
requests, so a client checking liveness gets an answer even while every handler
slot is busy. It cannot be replaced.

## Notifications

`@server.notification(method)` registers a coroutine `handler(params) -> None`.
A notification has no id and no reply; a raised exception is logged and
otherwise swallowed:

```python
@server.notification("notifications/initialized")
async def initialized(params):
    ...
```

An unregistered notification is ignored.

## Handler rules

- Handlers must be `async def`.
- Requests run concurrently and in isolation on the scheduler; each reply is
  enqueued on the single outbound stream.
- A request the client cancels (`notifications/cancelled`) interrupts its
  handler at an `await`, like any exception, and gets no reply. There is nothing
  to write for it.
- `server.run()` reads until stdin EOF, then drains in-flight handlers and their
  replies before returning.
