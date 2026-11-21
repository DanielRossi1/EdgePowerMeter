"""Automation API for AI agents and scripts (HTTP on localhost + MCP bridge).

Qt-free on purpose: `mcp_bridge` imports `app.api.schema` through this package,
and the bridge must start without a display. The Qt side lives in
`app.api.server` (`ApiServer`, the localhost HTTP server) and the tool
implementation in `app.api.service` (`ApiService`); import them from there.
"""

from .schema import API_VERSION, DEFAULT_PORT, TOOLS, TOOLS_BY_NAME, Tool

__all__ = ["API_VERSION", "DEFAULT_PORT", "TOOLS", "TOOLS_BY_NAME", "Tool"]
