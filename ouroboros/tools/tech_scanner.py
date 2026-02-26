"""Tech Scanner — systematic external knowledge acquisition.

Scans tech sources (HuggingFace, GitHub trending, HN) via MCP fetch,
extracts actionable insights, and stores them in knowledge base.
This is how Ouroboros stays aware of the evolving tech landscape.
"""

from __future__ import annotations

import json
import subprocess
import logging
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

SOURCES = {
    "huggingface_blog": "https://huggingface.co/blog",
    "github_trending": "https://github.com/trending?since=weekly",
    "hn_ai": "https://hn.algolia.com/api/v1/search?query=AI+agent+LLM&tags=story&hitsPerPage=15",
    "hn_mcp": "https://hn.algolia.com/api/v1/search?query=MCP+model+context+protocol&tags=story&hitsPerPage=10",
    "mcp_registry": "https://registry.modelcontextprotocol.io/",
}


def _fetch_via_mcp(url: str, max_length: int = 5000) -> str:
    """Fetch URL content via MCP fetch server."""
    try:
        result = subprocess.run(
            ["mcp-server-fetch"],
            input=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "fetch", "arguments": {"url": url, "max_length": max_length}}
            }),
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout[:max_length]
        return f"Fetch failed: {result.stderr[:200]}"
    except Exception as e:
        return f"Fetch error: {e}"


def scan_tech(ctx: ToolContext, *, source: str = "all", query: str = "") -> str:
    """Scan external tech sources for latest developments.

    Args:
        source: Which source to scan (huggingface_blog, github_trending,
                hn_ai, hn_mcp, mcp_registry, or 'all')
        query: Optional search query to filter results
    """
    if source == "all":
        sources_to_scan = list(SOURCES.keys())
    elif source in SOURCES:
        sources_to_scan = [source]
    else:
        return f"Unknown source: {source}. Available: {', '.join(SOURCES.keys())}, all"

    results = []
    for src in sources_to_scan:
        url = SOURCES[src]
        if query and "algolia" in url:
            url = url.replace("AI+agent+LLM", query.replace(" ", "+"))
        results.append(f"## {src}\nURL: {url}\n(Use MCP fetch tool to retrieve content)")

    return (
        "Tech scanner sources configured. To actually fetch content, "
        "use the MCP fetch tool directly:\n"
        "  mcp_call(server='fetch', tool='fetch', args='{\"url\": \"<url>\"}')\n\n"
        "Available sources:\n" + "\n\n".join(results)
    )


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="scan_tech",
            schema={
                "name": "scan_tech",
                "description": (
                    "List available tech scanning sources (HuggingFace, GitHub trending, "
                    "Hacker News AI/MCP). Returns URLs to fetch via MCP fetch tool. "
                    "Use this to discover what's new in the AI/ML ecosystem."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": "Source to scan: huggingface_blog, github_trending, hn_ai, hn_mcp, mcp_registry, or 'all'",
                            "default": "all",
                        },
                        "query": {
                            "type": "string",
                            "description": "Optional search query for HN sources",
                            "default": "",
                        },
                    },
                },
            },
            handler=scan_tech,
        ),
    ]
