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

## Notes

- All `Context` methods are `async`.
- Notifications are enqueued on the same single outbound stream as replies, so
  they never interleave with a handler's result.

> Server-initiated *requests* (sampling, elicitation, roots) are a later phase;
> `Context` will grow `sample` / `elicit` / `list_roots` then.
