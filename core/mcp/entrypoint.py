"""Standalone stdio MCP server for MCP hosts that launch local subprocesses."""

from core.mcp.server import create_mcp_server
from domains.air_us.bootstrap import AirUSServices

mcp = create_mcp_server(AirUSServices().tool_handlers())

if __name__ == "__main__":
    mcp.run()
