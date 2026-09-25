# Context

A handler -- a tool, a resource reader, or a raw
`@server.request`/`@server.notification` -- can declare a parameter annotated
`Context` (any name). The server injects the live connection for that call; the
parameter never appears in a tool's `inputSchema`. Detection is by the
annotation being `Context`, so the name is yours to choose.

```python
from nn_mcp_stdio import Context, Server

server = Server(name="jobs", version="0.1.0")


@server.tool()
async def crunch(rows: int, ctx: Context) -> str:
    """Crunch `rows` rows, reporting progress."""
    await ctx.info(f"starting {rows} rows", logger="crunch")
    for done in range(rows):
        await ctx.report_progress(done + 1, total=rows)
    return "done"
```

Through `Context` a handler talks back to the client mid-call.

## Logging

`notifications/message` -- `ctx.log(level, data, *, logger=None)` for the full
RFC 5424 level set, with `ctx.debug` / `ctx.info` / `ctx.warning` / `ctx.error`
shorthands. The server always declares the `logging` capability, so any handler
may log.

```python
await ctx.log("notice", {"stage": "parse", "rows": rows}, logger="crunch")
await ctx.warning("slow input", logger="crunch")
```

`data` is any JSON-serialisable value; `logger` names the emitting logger.

## Progress

`notifications/progress` -- `ctx.report_progress(progress, total=None,
message=None)`. It is a **no-op unless the request carried a `progressToken`**
(in `params._meta`) -- the client opts into progress, and there is nothing to
address without the token.

```python
await ctx.report_progress(done + 1, total=rows, message="crunching")
```

## Metadata

- `ctx.request_id` -- the id of the in-flight request (`None` inside a
  notification).
- `ctx.client` -- the client's `Implementation`, captured at `initialize`.

## Blocking work: `context.to_thread`

A blocking call -- a synchronous library, a long pure-Python calculation --
stops the server's event loop while it runs, and with it every other request.
Move it into a worker thread with `await context.to_thread(function, ...)`. The
handler stays `async def`; only the blocking part moves.

A `Context` cannot go along: it belongs to the event loop, and using it from a
thread raises `RuntimeError`. Instead, `to_thread` hands the function a
`ContextThreadSafe` -- through its parameter annotated `ContextThreadSafe`, any
name -- with the same methods as plain calls safe from the thread:

```python
from nn_mcp_stdio import Context, ContextThreadSafe, Server

server = Server(name="calc", version="0.1.0")


def simulate(build, rounds: int, *, ctx: ContextThreadSafe):
    for round_ in range(rounds):
        ctx.raise_if_cancelled()
        ...  # blocking work
        ctx.report_progress(round_ + 1, total=rounds)
    ctx.info("simulation done")
    return ...


@server.tool()
async def damage(build: Build, rounds: int, ctx: Context) -> DamageReport:
    return await ctx.to_thread(simulate, build, rounds)
```

- **The context is passed by keyword.** Positional arguments fill the other
  parameters first, so put the `ContextThreadSafe` parameter after them, or make
  it keyword-only as above. A function without one is called without it.
- **Messages keep their order**, and all of them precede the handler's reply.
- **A thread cannot be stopped from outside.** When the client cancels the
  request, the handler's `await` ends at once, but the thread runs on until it
  checks: `ctx.raise_if_cancelled()` raises `asyncio.CancelledError` (a
  `BaseException`, so `except Exception` does not swallow it), and
  `ctx.cancelled` is the flag for `while not ctx.cancelled:` loops. A function
  that never checks runs to the end; only its result is dropped.
- `ctx.request_id` and `ctx.client` are available as on `Context`.
- It keeps the event loop free; it does not speed work up. CPU-heavy Python is
  still bound by the GIL -- a process pool is yours to set up.

## Notes

- All `Context` methods are `async`; `ContextThreadSafe` has the same ones as
  plain methods.
- Notifications are enqueued on the same single outbound stream as replies, so
  they never interleave with a handler's result.

> Server-initiated *requests* (sampling, elicitation, roots) are a later phase;
> `Context` will grow `sample` / `elicit` / `list_roots` then.
