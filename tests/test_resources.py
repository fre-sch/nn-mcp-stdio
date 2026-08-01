"""Resource handlers: registration, resources/list, resources/read, capability."""

import base64
import json

import pytest

from nn_mcp_stdio import Context, Server
from nn_mcp_stdio.transport import MemoryTransport
from nn_mcp_types import content
from nn_mcp_types import resources as resource_types
from nn_mcp_types.content import BlobResourceContents, TextResourceContents


def encode(obj):
    return json.dumps(obj)


async def run(server, inbound):
    transport = MemoryTransport(inbound)
    await server.run(transport)
    return [json.loads(line) for line in transport.outbound]


def request(method, params=None, request_id=2):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return encode(message)


def read(uri, request_id=2):
    return request("resources/read", {"uri": uri}, request_id=request_id)


def library():
    server = Server(name="lib", version="0.1.0")

    @server.resource(
        resource_types.Resource(
            uri="file:///config.json",
            name="config",
            mime_type="application/json",
            description="The service configuration.",
        )
    )
    async def config() -> str:
        return '{"debug": true}'

    return server


async def test_list_reports_the_registered_resource():
    out = await run(library(), [request("resources/list", request_id=1)])
    (resource,) = out[0]["result"]["resources"]
    assert resource["uri"] == "file:///config.json"
    assert resource["name"] == "config"  # from the Resource definition
    assert resource["mimeType"] == "application/json"
    assert resource["description"] == "The service configuration."


async def test_decorator_carries_metadata_a_reader_cannot():
    # title/meta have no function-signature source; the Resource carries them.
    server = Server(name="lib", version="0.1.0")

    @server.resource(
        resource_types.Resource(
            uri="doc:///guide",
            name="guide",
            title="User Guide",
            meta={"section": "intro"},
        )
    )
    async def guide() -> str:
        return "..."

    out = await run(server, [request("resources/list", request_id=1)])
    (resource,) = out[0]["result"]["resources"]
    assert resource["title"] == "User Guide"
    assert resource["_meta"] == {"section": "intro"}


async def test_read_wraps_a_str_into_text_contents():
    out = await run(library(), [read("file:///config.json")])
    (contents,) = out[0]["result"]["contents"]
    assert contents == {
        "uri": "file:///config.json",
        "text": '{"debug": true}',
        "mimeType": "application/json",
    }


async def test_read_wraps_bytes_into_a_base64_blob():
    server = Server(name="lib", version="0.1.0")

    @server.resource(
        resource_types.Resource(
            uri="bin:///logo", name="logo", mime_type="image/png"
        )
    )
    async def logo() -> bytes:
        return b"\x89PNG\r\n"

    out = await run(server, [read("bin:///logo")])
    (contents,) = out[0]["result"]["contents"]
    assert contents["blob"] == base64.b64encode(b"\x89PNG\r\n").decode("ascii")
    assert contents["uri"] == "bin:///logo"
    assert contents["mimeType"] == "image/png"


async def test_read_passes_through_explicit_contents():
    server = Server(name="lib", version="0.1.0")

    @server.resource(resource_types.Resource(uri="doc:///two", name="two"))
    async def two() -> list:
        return [
            content.TextResourceContents(uri="doc:///two#a", text="a"),
            content.TextResourceContents(uri="doc:///two#b", text="b"),
        ]

    out = await run(server, [read("doc:///two")])
    uris = [part["uri"] for part in out[0]["result"]["contents"]]
    assert uris == ["doc:///two#a", "doc:///two#b"]


async def test_read_unknown_uri_is_resource_not_found():
    out = await run(library(), [read("file:///missing")])
    assert out[0]["error"]["code"] == -32002
    assert "file:///missing" in out[0]["error"]["message"]


async def test_reader_receives_context():
    server = Server(name="lib", version="0.1.0")

    @server.resource(resource_types.Resource(uri="who:///am/i", name="who"))
    async def who(ctx: Context) -> str:
        return str(ctx.request_id)

    out = await run(server, [read("who:///am/i", request_id=7)])
    (contents,) = out[0]["result"]["contents"]
    assert contents["text"] == "7"


async def test_initialize_advertises_resources_capability():
    initialize = encode(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "c", "version": "1"},
            },
        }
    )
    out = await run(library(), [initialize])
    assert "resources" in out[0]["result"]["capabilities"]


async def test_no_resources_no_capability():
    initialize = encode(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "c", "version": "1"},
            },
        }
    )
    out = await run(Server(name="bare", version="0.1.0"), [initialize])
    assert "resources" not in out[0]["result"]["capabilities"]


def test_non_async_reader_is_rejected():
    server = Server(name="lib", version="0.1.0")
    with pytest.raises(TypeError):

        @server.resource(resource_types.Resource(uri="sync:///no", name="no"))
        def config():
            return "x"


async def test_bad_return_is_a_type_error():
    server = Server(name="lib", version="0.1.0")

    @server.resource(resource_types.Resource(uri="num:///bad", name="bad"))
    async def bad() -> int:
        return 42

    built, _ = server._resource_router.match("num:///bad")
    with pytest.raises(TypeError):
        await built.read("num:///bad", {})


# -- static resources: add_resource_from_literal ---------------------------


def definition(uri, **fields):
    return resource_types.Resource(uri=uri, name="static", **fields)


async def test_literal_str_is_listed_verbatim_and_read_as_text():
    server = Server(name="lib", version="0.1.0")
    server.add_resource_from_literal(
        definition(
            "file:///notes.md",
            title="Notes",
            mime_type="text/markdown",
        ),
        "# hello",
    )

    listed = await run(server, [request("resources/list", request_id=1)])
    (resource,) = listed[0]["result"]["resources"]
    assert resource["title"] == "Notes"  # full definition, verbatim
    assert resource["mimeType"] == "text/markdown"

    out = await run(server, [read("file:///notes.md")])
    (contents,) = out[0]["result"]["contents"]
    assert contents == {
        "uri": "file:///notes.md",
        "text": "# hello",
        "mimeType": "text/markdown",
    }


async def test_literal_bytes_is_read_as_base64_blob():
    server = Server(name="lib", version="0.1.0")
    server.add_resource_from_literal(
        definition("bin:///logo", mime_type="image/png"), b"\x89PNG\r\n"
    )

    out = await run(server, [read("bin:///logo")])
    (contents,) = out[0]["result"]["contents"]
    assert contents["blob"] == base64.b64encode(b"\x89PNG\r\n").decode("ascii")
    assert contents["mimeType"] == "image/png"


def test_literal_rejects_a_non_str_bytes_content():
    server = Server(name="lib", version="0.1.0")
    with pytest.raises(TypeError):
        server.add_resource_from_literal(definition("num:///bad"), 42)


# -- static resources: add_resource_from_path ------------------------------


async def test_path_without_a_classifier_is_a_blob(tmp_path):
    server = Server(name="lib", version="0.1.0")
    file = tmp_path / "data.bin"
    file.write_bytes(b"\x00\x01\x02")
    server.add_resource_from_path(
        definition("file:///data.bin", mime_type="application/octet-stream"),
        file,
    )

    out = await run(server, [read("file:///data.bin")])
    (contents,) = out[0]["result"]["contents"]
    assert contents["blob"] == base64.b64encode(b"\x00\x01\x02").decode("ascii")
    assert contents["mimeType"] == "application/octet-stream"


async def test_path_classifier_sets_type_and_mime(tmp_path):
    server = Server(name="lib", version="0.1.0")
    file = tmp_path / "config.json"
    file.write_text('{"debug": true}', encoding="utf-8")

    def describe(path, data):
        return "application/json", TextResourceContents

    server.add_resource_from_path(
        # definition advertises nothing; the classifier fills the read type
        definition("file:///config.json"),
        file,
        describe_contents=describe,
    )

    listed = await run(server, [request("resources/list", request_id=1)])
    (resource,) = listed[0]["result"]["resources"]
    assert (
        "mimeType" not in resource
    )  # the listing hint stays as defined (none)

    out = await run(server, [read("file:///config.json")])
    (contents,) = out[0]["result"]["contents"]
    assert contents == {
        "uri": "file:///config.json",
        "text": '{"debug": true}',
        "mimeType": "application/json",
    }


async def test_path_is_read_lazily_on_each_read(tmp_path):
    server = Server(name="lib", version="0.1.0")
    file = tmp_path / "live.txt"
    file.write_text("first", encoding="utf-8")
    server.add_resource_from_path(
        definition("file:///live.txt"),
        file,
        describe_contents=lambda path, data: (
            "text/plain",
            TextResourceContents,
        ),
    )

    first = await run(server, [read("file:///live.txt")])
    assert first[0]["result"]["contents"][0]["text"] == "first"

    file.write_text("second", encoding="utf-8")  # changes between reads
    second = await run(server, [read("file:///live.txt")])
    assert second[0]["result"]["contents"][0]["text"] == "second"


async def test_path_classifier_bad_block_type_is_a_type_error(tmp_path):
    server = Server(name="lib", version="0.1.0")
    file = tmp_path / "x.dat"
    file.write_bytes(b"x")
    server.add_resource_from_path(
        definition("file:///x.dat"),
        file,
        describe_contents=lambda path, data: ("text/plain", str),
    )

    built, _ = server._resource_router.match("file:///x.dat")
    with pytest.raises(TypeError):
        await built.read("file:///x.dat", {})


# -- resource templates: @server.resource_template -------------------------


def template(uri_template, **fields):
    return resource_types.ResourceTemplate(
        uri_template=uri_template, name="files", **fields
    )


def templates_list(request_id=1):
    return request("resources/templates/list", request_id=request_id)


async def test_template_is_listed_verbatim():
    server = Server(name="lib", version="0.1.0")

    @server.resource_template(
        template(
            "file:///project/{path}",
            title="Project files",
            mime_type="text/plain",
        )
    )
    async def project_file(path) -> str:
        return path

    out = await run(server, [templates_list()])
    (listed,) = out[0]["result"]["resourceTemplates"]
    assert listed["uriTemplate"] == "file:///project/{path}"
    assert listed["title"] == "Project files"
    assert listed["mimeType"] == "text/plain"


async def test_read_of_a_matching_uri_injects_extracted_variables():
    server = Server(name="lib", version="0.1.0")

    @server.resource_template(template("file:///project/{name}"))
    async def project_file(name) -> str:
        return f"contents of {name}"

    out = await run(server, [read("file:///project/notes.txt")])
    (contents,) = out[0]["result"]["contents"]
    assert contents["text"] == "contents of notes.txt"
    # The wrapped return is typed by the concrete request URI, not the template.
    assert contents["uri"] == "file:///project/notes.txt"


async def test_wildcard_variable_spans_path_segments():
    server = Server(name="lib", version="0.1.0")

    @server.resource_template(template("file:///project/{path*}"))
    async def project_file(path) -> str:
        return path

    out = await run(server, [read("file:///project/notes/todo.txt")])
    (contents,) = out[0]["result"]["contents"]
    # `{path*}` is a wildcard: its value may cross `/`.
    assert contents["text"] == "notes/todo.txt"


async def test_template_reader_receives_context():
    server = Server(name="lib", version="0.1.0")

    @server.resource_template(template("id:///{item}"))
    async def by_id(item, ctx: Context) -> str:
        return f"{item}@{ctx.request_id}"

    out = await run(server, [read("id:///42", request_id=9)])
    (contents,) = out[0]["result"]["contents"]
    assert contents["text"] == "42@9"


async def test_direct_resource_shadows_an_overlapping_template():
    server = Server(name="lib", version="0.1.0")

    @server.resource_template(template("file:///project/{path}"))
    async def project_file(path) -> str:
        return f"template: {path}"

    @server.resource(
        resource_types.Resource(
            uri="file:///project/last_build", name="last_build"
        )
    )
    async def last_build() -> str:
        return "direct"

    # The direct resource is maximally specific -> it wins the overlap.
    shadowed = await run(server, [read("file:///project/last_build")])
    assert shadowed[0]["result"]["contents"][0]["text"] == "direct"

    # A sibling URI the direct resource does not claim still routes to template.
    templated = await run(server, [read("file:///project/other")])
    assert templated[0]["result"]["contents"][0]["text"] == "template: other"


async def test_read_with_no_matching_route_is_resource_not_found():
    server = Server(name="lib", version="0.1.0")

    @server.resource_template(template("file:///project/{path}"))
    async def project_file(path) -> str:
        return path

    out = await run(server, [read("other:///nope")])
    assert out[0]["error"]["code"] == -32002


async def test_template_advertises_the_resources_capability():
    server = Server(name="lib", version="0.1.0")

    @server.resource_template(template("file:///project/{path}"))
    async def project_file(path) -> str:
        return path

    initialize = encode(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "c", "version": "1"},
            },
        }
    )
    out = await run(server, [initialize])
    assert "resources" in out[0]["result"]["capabilities"]


def test_non_async_template_reader_is_rejected():
    server = Server(name="lib", version="0.1.0")
    with pytest.raises(TypeError):

        @server.resource_template(template("file:///project/{path}"))
        def project_file(path):
            return path


def test_template_with_an_unsupported_operator_is_rejected_at_registration():
    server = Server(name="lib", version="0.1.0")
    with pytest.raises(Exception):
        # `{+var}` is outside the built-in subset -> the router refuses the route.
        @server.resource_template(template("file:///project/{+path}"))
        async def project_file(path) -> str:
            return path


def test_parameterless_template_is_rejected():
    # A literal uri_template carries no parameter -- it is a resource, not a
    # template (nothing to expand or complete). Rejected at registration.
    server = Server(name="lib", version="0.1.0")
    with pytest.raises(ValueError):

        @server.resource_template(template("file:///project/pinned"))
        async def pinned() -> str:
            return "x"


async def test_query_only_template_is_accepted():
    # A `{?q}` block is a parameter (a client can complete it), so the template
    # is valid even with a literal path.
    server = Server(name="lib", version="0.1.0")

    @server.resource_template(template("search://items{?q}"))
    async def search(q="") -> str:
        return f"query={q}"

    listed = await run(server, [templates_list()])
    (template_def,) = listed[0]["result"]["resourceTemplates"]
    assert template_def["uriTemplate"] == "search://items{?q}"

    out = await run(server, [read("search://items?q=hello")])
    assert out[0]["result"]["contents"][0]["text"] == "query=hello"


async def test_no_templates_are_listed_when_none_registered():
    out = await run(library(), [templates_list()])
    assert out[0]["result"]["resourceTemplates"] == []


async def test_lists_partition_direct_and_template_registrations():
    # Both list endpoints derive from the one router; each surfaces only its kind.
    server = Server(name="lib", version="0.1.0")

    @server.resource(
        resource_types.Resource(uri="file:///project/pinned", name="pinned")
    )
    async def pinned() -> str:
        return "direct"

    @server.resource_template(template("file:///project/{path}"))
    async def project_file(path) -> str:
        return path

    listed = await run(server, [request("resources/list", request_id=1)])
    (resource,) = listed[0]["result"]["resources"]
    assert resource["uri"] == "file:///project/pinned"
    assert "uriTemplate" not in resource

    templates = await run(server, [templates_list()])
    (template_def,) = templates[0]["result"]["resourceTemplates"]
    assert template_def["uriTemplate"] == "file:///project/{path}"


def test_server_keeps_no_shadow_resource_dicts():
    # The router owns the route set; no parallel per-kind bookkeeping remains.
    server = Server(name="lib", version="0.1.0")
    assert not hasattr(server, "_resources")
    assert not hasattr(server, "_resource_templates")
