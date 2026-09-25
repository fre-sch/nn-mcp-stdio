"""context.elicit: the server asks the user for a form, the client answers."""

import asyncio
import dataclasses
import datetime
import enum
import logging
import threading
import typing

import pytest
from jsonschema.exceptions import ValidationError
from support import PAUSE, cancel, request, run

from nn_mcp_types.schema import Choices, SchemaAnnotation

from nn_mcp_stdio import (
    ClientError,
    Context,
    ContextThreadSafe,
    ElicitationNotSupportedError,
    Server,
)
from nn_mcp_stdio.elicitation import requested_schema


@dataclasses.dataclass
class Account:
    name: str
    age: int = 0


def initialize(capabilities):
    return {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": capabilities,
            "clientInfo": {"name": "test", "version": "0"},
        },
    }


FORMS = initialize({"elicitation": {}})


def call(name, request_id=7):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": {}},
    }


def answer(result, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def accept(content):
    return answer({"action": "accept", "content": content})


def elicit_server(events=None):
    events = [] if events is None else events
    server = Server(name="demo", version="0.1.0")

    @server.tool()
    async def ask(context: Context) -> str:
        try:
            action, account = await context.elicit("Which account?", Account)
        except asyncio.CancelledError:
            events.append(("cancelled", server._input_closed))
            raise
        except Exception as error:
            events.append(error)
            raise
        return f"{action}: {account}"

    return server


def by_id(out, request_id):
    return next(message for message in out if message.get("id") == request_id)


def text(reply):
    return reply["result"]["content"][0]["text"]


def elicitations(out):
    return [m for m in out if m.get("method") == "elicitation/create"]


# --- the form a dataclass becomes ------------------------------------------


@dataclasses.dataclass
class Everything:
    name: typing.Annotated[
        str, SchemaAnnotation(min_length=1, max_length=20, pattern="^[a-z]+$")
    ]
    email: typing.Annotated[str, SchemaAnnotation(format="email")]
    age: typing.Annotated[int, SchemaAnnotation(minimum=0, maximum=150)]
    height: float
    subscribed: bool
    dough: typing.Annotated[str, Choices({"thin": "Thin", "deep": "Deep"})]
    size: typing.Annotated[str, Choices(["s", "m", "l"])]
    toppings: typing.Annotated[
        list[str], Choices({"olive": "Olives"}, max_items=3)
    ]
    extras: typing.Annotated[list[str], Choices(["cheese", "basil"])]
    note: typing.Annotated[
        str, SchemaAnnotation(description="Anything else")
    ] = ""


def test_a_flat_dataclass_becomes_the_requested_schema():
    schema = requested_schema(Everything)
    assert schema["type"] == "object"
    assert "title" not in schema  # the class name is not shown to users
    assert schema["properties"]["name"]["pattern"] == "^[a-z]+$"
    assert schema["properties"]["extras"]["items"] == {
        "type": "string",
        "enum": ["cheese", "basil"],
    }
    assert "note" not in schema["required"]


class Colour(enum.Enum):
    RED = "red"


@dataclasses.dataclass
class Nested:
    account: Account


@dataclasses.dataclass
class WithEnum:
    colour: Colour


@dataclasses.dataclass
class WithLiteral:
    size: typing.Literal["s", "m"]


@dataclasses.dataclass
class WithOptional:
    nickname: str | None = None


@dataclasses.dataclass
class WithPlainList:
    tags: list[str]


@dataclasses.dataclass
class WithHostname:
    host: typing.Annotated[str, SchemaAnnotation(format="hostname")]


@dataclasses.dataclass
class WithUniqueItems:
    tags: typing.Annotated[list[str], Choices(["a", "b"], unique_items=True)]


@dataclasses.dataclass
class WithExclusiveMinimum:
    count: typing.Annotated[int, SchemaAnnotation(exclusive_minimum=0)]


@pytest.mark.parametrize(
    "form",
    [
        Nested,
        WithEnum,
        WithLiteral,
        WithOptional,
        WithPlainList,
        WithHostname,
        WithExclusiveMinimum,
        WithUniqueItems,
    ],
)
def test_a_form_mcp_does_not_allow_raises(form):
    with pytest.raises(TypeError, match=form.__name__):
        requested_schema(form)


# --- the round trip ---------------------------------------------------------


async def test_accepted_content_becomes_an_instance_of_the_form():
    out = await run(
        elicit_server(), [FORMS, call("ask"), accept({"name": "main"})]
    )
    [sent] = elicitations(out)
    assert sent["id"] == 1
    assert sent["params"]["message"] == "Which account?"
    assert sent["params"]["requestedSchema"]["required"] == ["name"]
    assert text(by_id(out, 7)) == "accept: Account(name='main', age=0)"


@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_decline_and_cancel_carry_no_content(action):
    out = await run(
        elicit_server(), [FORMS, call("ask"), answer({"action": action})]
    )
    assert text(by_id(out, 7)) == f"{action}: None"


async def test_content_that_does_not_fit_raises_validation_error():
    events = []
    await run(elicit_server(events), [FORMS, call("ask"), accept({"name": 42})])
    assert isinstance(events[0], ValidationError)


@dataclasses.dataclass
class When:
    day: datetime.date


def date_server(events):
    server = Server(name="demo", version="0.1.0")

    @server.tool()
    async def ask(context: Context) -> str:
        try:
            return str(await context.elicit("When?", When))
        except ValidationError as error:
            events.append(error)
            raise

    return server


async def test_a_date_field_is_built_from_its_string():
    inbound = [FORMS, call("ask"), accept({"day": "2026-02-28"})]
    out = await run(date_server([]), inbound)
    assert "datetime.date(2026, 2, 28)" in text(by_id(out, 7))


async def test_a_date_that_is_no_date_raises_validation_error():
    events = []
    inbound = [FORMS, call("ask"), accept({"day": "2026-02-30"})]
    await run(date_server(events), inbound)
    assert isinstance(events[0], ValidationError)


async def test_a_malformed_result_raises_validation_error():
    events = []
    await run(
        elicit_server(events), [FORMS, call("ask"), answer({"action": "maybe"})]
    )
    assert isinstance(events[0], ValidationError)


async def test_an_error_answer_raises_client_error():
    events = []
    error = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32601, "message": "no forms here"},
    }
    await run(elicit_server(events), [FORMS, call("ask"), error])
    assert isinstance(events[0], ClientError)
    assert (events[0].code, events[0].message) == (-32601, "no forms here")


# --- capability ---------------------------------------------------------------


@pytest.mark.parametrize(
    "capabilities",
    [None, {}, {"elicitation": {"url": {}}}],
    ids=["no initialize", "no elicitation", "url mode only"],
)
async def test_without_form_elicitation_nothing_is_sent(capabilities):
    events = []
    inbound = [call("ask")]
    if capabilities is not None:
        inbound.insert(0, initialize(capabilities))
    out = await run(elicit_server(events), inbound)
    assert isinstance(events[0], ElicitationNotSupportedError)
    assert elicitations(out) == []


@pytest.mark.parametrize(
    "elicitation", [{}, {"form": {}}, {"form": {}, "url": {}}]
)
async def test_form_elicitation_is_declared(elicitation):
    inbound = [
        initialize({"elicitation": elicitation}),
        call("ask"),
        accept({"name": "main"}),
    ]
    out = await run(elicit_server(), inbound)
    assert text(by_id(out, 7)).startswith("accept")


async def test_a_bad_form_raises_before_the_capability_check():
    events = []
    server = Server(name="demo", version="0.1.0")

    @server.tool()
    async def ask(context: Context) -> str:
        try:
            await context.elicit("?", Nested)
        except TypeError as error:
            events.append(error)
        return "done"

    await run(server, [call("ask")])  # no initialize: no capability either
    assert isinstance(events[0], TypeError)


async def test_malformed_capabilities_fail_initialize():
    out = await run(elicit_server(), [initialize({"elicitation": True})])
    assert out[0]["error"]["code"] == -32602


# --- responses with no pending request -------------------------------------


async def test_a_response_to_no_pending_request_is_logged_and_dropped(caplog):
    caplog.set_level(logging.WARNING, logger="nn_mcp_stdio")
    out = await run(
        elicit_server(),
        [answer({"action": "accept"}, request_id=99), request(5, "ping")],
    )
    assert out == [{"jsonrpc": "2.0", "id": 5, "result": {}}]
    assert "no pending request" in caplog.text


# --- cancellation and EOF -----------------------------------------------------


async def test_cancelling_the_call_drops_the_elicitation(caplog):
    caplog.set_level(logging.WARNING, logger="nn_mcp_stdio")
    events = []
    server = elicit_server(events)
    inbound = [FORMS, call("ask"), cancel(7), accept({"name": "late"})]
    out = await run(server, inbound)
    assert events == [("cancelled", False)]  # before EOF
    assert [m.get("method") for m in out if "id" in m and m["id"] != 0] == [
        "elicitation/create"
    ]  # no reply to 7, and no notifications/cancelled either
    assert not any(m.get("method") == "notifications/cancelled" for m in out)
    assert server._pending == {}
    assert "no pending request" in caplog.text  # the late answer


async def test_eof_cancels_a_waiting_elicitation():
    events = []
    server = elicit_server(events)
    await asyncio.wait_for(run(server, [FORMS, call("ask")]), timeout=5)
    assert events == [("cancelled", True)]
    assert server._pending == {}


async def test_an_elicitation_after_eof_is_cancelled_at_once():
    events = []
    server = Server(name="demo", version="0.1.0")

    @server.tool()
    async def ask_late(context: Context) -> str:
        await asyncio.sleep(3 * PAUSE)  # stdin closes meanwhile
        try:
            await context.elicit("?", Account)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise
        return "unreachable"

    out = await asyncio.wait_for(
        run(server, [FORMS, call("ask_late")]), timeout=5
    )
    assert events == ["cancelled"]
    assert elicitations(out) == []


# --- from a thread ------------------------------------------------------------


def thread_server(events):
    server = Server(name="demo", version="0.1.0")
    finished = threading.Event()

    def choose(context: ContextThreadSafe):
        try:
            return context.elicit("Which account?", Account)
        except asyncio.CancelledError:
            events.append(("thread cancelled", server._input_closed))
            raise
        finally:
            finished.set()

    @server.tool()
    async def ask(context: Context) -> str:
        action, account = await context.to_thread(choose)
        return f"{action}: {account}"

    return server, finished


async def test_a_thread_elicits_and_blocks_for_the_answer():
    server, _ = thread_server([])
    out = await run(server, [FORMS, call("ask"), accept({"name": "main"})])
    assert text(by_id(out, 7)) == "accept: Account(name='main', age=0)"


async def test_cancelling_the_call_ends_a_thread_waiting_on_elicit():
    events = []
    server, finished = thread_server(events)
    out = await run(server, [FORMS, call("ask"), cancel(7), request(5, "ping")])
    await asyncio.to_thread(finished.wait, 2)
    assert events == [("thread cancelled", False)]  # before EOF
    assert 7 not in [m.get("id") for m in out]
    assert server._pending == {}
