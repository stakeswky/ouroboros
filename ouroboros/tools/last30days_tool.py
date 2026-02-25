"""last30days — multi-source research (Web + X + YouTube) with 30-day aggregation."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

_SCRIPT = "/root/ouroboros/tools/last30days/last30days.py"


def _research(ctx: ToolContext, query: str, days: int = 30, depth: str = "default",
              sources: str = "all") -> str:
    """Run a multi-source research query."""
    cmd = [sys.executable, _SCRIPT, query,
           "--days", str(days), "--depth", depth,
           "--sources", sources, "--emit", "json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = result.stdout
        if len(output) > 15000:
            # Parse and return summary only
            try:
                data = json.loads(output)
                return json.dumps({
                    "context_snippet_md": data.get("context_snippet_md", ""),
                    "best_practices": data.get("best_practices", [])[:5],
                    "web_count": len(data.get("web", [])),
                    "x_count": len(data.get("x", [])),
                    "youtube_count": len(data.get("youtube", [])),
                    "top_web": [{"title": r.get("title"), "url": r.get("url"), "score": r.get("score")} for r in data.get("web", [])[:5]],
                    "top_x": [{"text": r.get("text", "")[:200], "score": r.get("score")} for r in data.get("x", [])[:5]],
                }, ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                return output[:15000]
        return output
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Research timed out after 120s"})
    except Exception as e:
        return json.dumps({"error": repr(e)})


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("research", {
            "name": "research",
            "description": "Deep multi-source research (Brave Web + X/Twitter + YouTube). Aggregates and scores results from the last N days. Use for: technology trends, community discussions, best practices research before making evolution decisions.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Research query"},
                "days": {"type": "integer", "description": "Lookback days (default 30)"},
                "depth": {"type": "string", "enum": ["quick", "default", "deep"], "description": "Research depth"},
                "sources": {"type": "string", "enum": ["all", "web", "x", "youtube"], "description": "Which sources to query"},
            }, "required": ["query"]},
        }, _research, timeout_sec=180),
    ]
