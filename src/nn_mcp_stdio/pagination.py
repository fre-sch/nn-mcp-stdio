"""Offset pagination for the list endpoints.

The cursor is an offset into a stable ordering, sent as its decimal string.
Best-effort across changes to the set, like SQL offset; see the wiki's
`decisions/pagination.md`.
"""

import typing

from nn_mcp_stdio import errors

Item = typing.TypeVar("Item")


def page(
    items: list[Item], cursor: str | None, page_size: int | None
) -> tuple[list[Item], str | None]:
    """The page of `items` a cursor points at, and the cursor after it.

    `page_size=None` disables pagination: everything from the cursor on, and no
    next cursor. The last page has no next cursor either.
    """
    offset = offset_of(cursor)
    if page_size is None:
        return items[offset:], None
    end = offset + page_size
    next_cursor = str(end) if end < len(items) else None
    return items[offset:end], next_cursor


def offset_of(cursor: str | None) -> int:
    """The offset a cursor encodes; a malformed one is invalid params."""
    if cursor is None:
        return 0
    if isinstance(cursor, str) and cursor.isascii() and cursor.isdigit():
        return int(cursor)
    raise errors.invalid_params(f"invalid cursor: {cursor!r}")
