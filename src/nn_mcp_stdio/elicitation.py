"""Elicitation forms: the restricted schema a dataclass becomes, and back.

`context.elicit(message, Form)` asks the user to fill in `Form`, an ordinary
dataclass. MCP allows only a flat object of primitive fields as an elicitation's
`requestedSchema`, so the schema generated from `Form` is checked against that
restriction before anything is sent, and the accepted content is validated
against it before it becomes a `Form`. The keywords permitted are the written
specification's: its `schema.ts` set plus `pattern`
(wiki: decisions/elicitation.md).
"""

import typing

from jsonschema.validators import Draft202012Validator

from nn_mcp_types import elicitation, lifecycle
from nn_mcp_types.schema import get_schema
from nn_mcp_types.wire import from_wire

_RESULT_VALIDATOR = Draft202012Validator(get_schema(elicitation.ElicitResult))

_FORMATS = {"email", "uri", "date", "date-time"}

_ANNOTATIONS = {"type", "title", "description", "default"}

# Keywords a field may carry, by its `type`. A string field carries `enum` or
# `oneOf` when it is a single-select `Choices`.
_FIELD_KEYWORDS = {
    "string": _ANNOTATIONS
    | {"minLength", "maxLength", "pattern", "format", "enum", "oneOf"},
    "number": _ANNOTATIONS | {"minimum", "maximum"},
    "integer": _ANNOTATIONS | {"minimum", "maximum"},
    "boolean": _ANNOTATIONS,
    "array": _ANNOTATIONS | {"items", "minItems", "maxItems"},
}

# The two item shapes of a multi-select `Choices`: untitled and titled.
_ITEMS_KEYWORDS = ({"type", "enum"}, {"anyOf"})

# The top level dc_schema generates for a dataclass; `title` is its class name
# and is not sent.
_FORM_KEYWORDS = {"$schema", "type", "title", "properties", "required"}


class ElicitationNotSupportedError(Exception):
    """The client did not declare the `elicitation` capability for forms."""


def supports_forms(capabilities: lifecycle.ClientCapabilities | None) -> bool:
    """Whether the client declared form-mode elicitation.

    An empty `elicitation` capability means form mode; one that declares only
    `url` does not include it.
    """
    if capabilities is None or capabilities.elicitation is None:
        return False
    declared = capabilities.elicitation
    return declared.form is not None or declared.url is None


def requested_schema(form: type) -> dict:
    """The `requestedSchema` for the dataclass `form`, or `TypeError`."""
    schema = get_schema(form)
    unknown = set(schema) - _FORM_KEYWORDS
    if unknown:
        raise TypeError(
            f"{form.__name__} is not a flat form: it generates "
            f"{sorted(unknown)} -- nested dataclasses, enums and other named "
            "types are not allowed"
        )
    for name, field in schema["properties"].items():
        check_field(form, name, field)
    schema.pop("title")
    return schema


def check_field(form: type, name: str, field: dict) -> None:
    """Raise `TypeError` unless `field` is a form field MCP allows."""
    where = f"{form.__name__}.{name}"
    field_type = field.get("type")
    if not isinstance(field_type, str) or field_type not in _FIELD_KEYWORDS:
        raise TypeError(
            f"{where} is not a form field: fields are str, int, float, bool, "
            "or a Choices over str or list[str]; an optional field takes a "
            "default of its own type"
        )
    unknown = set(field) - _FIELD_KEYWORDS[field_type]
    if unknown:
        raise TypeError(
            f"{where} uses keywords forms do not allow: {sorted(unknown)}"
        )
    if "format" in field and field["format"] not in _FORMATS:
        raise TypeError(
            f"{where} has format {field['format']!r}; forms allow "
            f"{sorted(_FORMATS)}"
        )
    if (
        field_type == "array"
        and set(field.get("items", {})) not in _ITEMS_KEYWORDS
    ):
        raise TypeError(
            f"{where} is a list but not a multi-select: annotate list[str] "
            "with Choices"
        )


def answer(result: dict, form: type, schema: dict) -> tuple:
    """The `(action, content)` a handler receives for the client's `result`.

    Raises `jsonschema.ValidationError` when the result is malformed or the
    accepted content does not fit `schema`.
    """
    _RESULT_VALIDATOR.validate(result)
    elicited = from_wire(elicitation.ElicitResult, result)
    if elicited.action != "accept":
        return elicited.action, None
    # `format` is asserted where jsonschema can check it without extras
    # (`date`, `email`): a form's `date` field is built with `fromisoformat`.
    Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    ).validate(elicited.content)
    return "accept", from_wire(form, elicited.content)
