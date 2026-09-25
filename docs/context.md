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

Through `Context` a handler talks back to the client mid-call, and can ask the
user for input.

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

## Elicitation: asking the user

`elicitation/create` -- `await ctx.elicit(message, Form)` asks the user,
through the client, to fill in `Form`, an ordinary dataclass, and waits for the
answer:

```python
import dataclasses
import typing

from nn_mcp_types.schema import Choices, SchemaAnnotation

from nn_mcp_stdio import Context, Server

server = Server(name="shop", version="0.1.0")


@dataclasses.dataclass
class Delivery:
    street: str
    floor: typing.Annotated[int, SchemaAnnotation(minimum=0)] = 0
    slot: typing.Annotated[
        str, Choices({"am": "Morning", "pm": "Afternoon"})
    ] = "am"


@server.tool()
async def order(item: str, ctx: Context) -> str:
    action, delivery = await ctx.elicit(f"Where should {item} go?", Delivery)
    if action != "accept":
        return f"no order: the user chose {action}"
    return f"{item} to {delivery.street}, {delivery.slot}"
```

It returns `("accept", Delivery(...))`, `("decline", None)` or
`("cancel", None)`. The accepted content is validated against the form and
built into an instance of it.

**A form is flat.** MCP allows only primitive fields:

| field | declared as |
|---|---|
| text | `str`; `SchemaAnnotation` may add `min_length`, `max_length`, `pattern`, `format` (`email`, `uri`, `date`, `date-time`) |
| number | `int` or `float`; `SchemaAnnotation` may add `minimum`, `maximum` |
| yes/no | `bool` |
| pick one | `Annotated[str, Choices(...)]` -- a mapping for titled choices, a list for plain ones |
| pick several | `Annotated[list[str], Choices(...)]`; `min_items`, `max_items` on the `Choices` |

`title`, `description` and a default work on every field. A field with a
default is optional. `date` and `datetime` fields work too -- they are strings
with a `format`. Anything else -- nested dataclasses, enums, `Literal`,
`X | None`, a list without `Choices` -- raises `TypeError` at the call, before
anything is sent. Forms have no null, so an optional field takes a default of
its own type (`nickname: str = ""`), not `None`.

**What else it raises:**

- `ElicitationNotSupportedError` -- the client did not declare the
  `elicitation` capability for forms at `initialize`. Nothing is sent.
- `jsonschema.ValidationError` -- the client's answer does not fit the form.
  A client may not enforce every keyword (`pattern`, say), so the user can
  submit what the form rejects; elicit again if it matters.
- `ClientError` -- the client answered with a JSON-RPC error; `code` and
  `message` are its.

**Waiting.** There is no timeout: a human may take their time. The wait ends
with `asyncio.CancelledError` when the client cancels the request, or when
stdin closes -- nobody is left to answer then. Nothing is sent to the client
for an elicitation left unanswered.

Only form mode is supported; URL mode -- the one the specification requires for
secrets -- is not, so never elicit credentials.

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
- **`ctx.elicit(message, Form)` blocks the thread** until the user answers,
  and returns what `Context.elicit` does. A cancelled request ends the wait
  with `asyncio.CancelledError`.
- `ctx.request_id` and `ctx.client` are available as on `Context`.
- It keeps the event loop free; it does not speed work up. CPU-heavy Python is
  still bound by the GIL -- a process pool is yours to set up.

## Notes

- All `Context` methods are `async`; `ContextThreadSafe` has the same ones as
  plain methods.
- Notifications and elicitation requests are enqueued on the same single
  outbound stream as replies, so they never interleave with a handler's result.
- Sampling and roots are deprecated by MCP `2026-07-28` and not provided.
