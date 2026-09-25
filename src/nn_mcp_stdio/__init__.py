"""No nonsense stdio MCP server: expose handler functions, run over stdio."""

import importlib.metadata

from nn_mcp_stdio.context import Context, ContextThreadSafe
from nn_mcp_stdio.server import Server

# Single source of truth is pyproject.toml's project.version. Read it back from
# the installed distribution's metadata rather than duplicating the string here.
__version__ = importlib.metadata.version("nn-mcp-stdio")

__all__ = ["Server", "Context", "ContextThreadSafe"]
