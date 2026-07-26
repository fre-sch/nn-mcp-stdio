"""No nonsense stdio MCP server: expose handler functions, run over stdio."""

from nn_mcp_stdio.context import Context
from nn_mcp_stdio.server import Server

__version__ = "0.1.0"

__all__ = ["Server", "Context"]
