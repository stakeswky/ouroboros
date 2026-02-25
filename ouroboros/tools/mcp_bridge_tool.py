"""MCP Bridge — connect to any MCP server and call its tools."""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

_BRIDGE = "/root/ouroboros/tools/mcp-bridge/mcp-bridge.mjs"


def _mcp_list(ctx: ToolContext, server: str) -> str:
    """List tools available on an MCP server."""
    try:
        result = subprocess.run(
            ["node", _BRIDGE, "list", server],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout or result.stderr
    except Exception as e:
        return json.dumps({"error": repr(e)})


def _mcp_call(ctx: ToolContext, server: str, tool: str, args: str = "{}") -> str:
    """Call a tool on an MCP server."""
    try:
        parsed = json.loads(args) if args else {}
    except json.JSONDecodeError:
        return json.dumps({"error": f"Invalid JSON args: {args}"})
    try:
        result = subprocess.run(
            ["node", _BRIDGE, "call", server, tool, json.dumps(parsed)],
            capture_output=True, text=True, timeout=60,
        )
        output = result.stdout[-10000:] if len(result.stdout) > 10000 else result.stdout
        return output or result.stderr
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "MCP call timed out after 60s"})
    except Exception as e:
        return json.dumps({"error": repr(e)})


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("mcp_list", {
            "name": "mcp_list",
            "description": "List tools available on an MCP server. Servers configured in mcp-servers.json: everything (demo). Add new servers by editing the config.",
            "parameters": {"type": "object", "properties": {
                "server": {"type": "string", "description": "MCP server name from config"},
            }, "required": ["server"]},
        }, _mcp_list),
        ToolEntry("mcp_call", {
            "name": "mcp_call",
            "description": "Call a tool on an MCP server. Use mcp_list first to discover available tools and their schemas.",
            "parameters": {"type": "object", "properties": {
                "server": {"type": "string", "description": "MCP server name"},
                "tool": {"type": "string", "description": "Tool name to call"},
                "args": {"type": "string", "description": "JSON string of tool arguments"},
            }, "required": ["server", "tool"]},
        }, _mcp_call),
    ]
