"""Web search tool — uses httpx, no openai SDK."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import httpx

from ouroboros.tools.registry import ToolContext, ToolEntry


def _web_search(ctx: ToolContext, query: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return json.dumps({"error": "OPENAI_API_KEY not set; web_search unavailable."})
    try:
        base_url = os.environ.get("OUROBOROS_LLM_BASE_URL", "https://oogg.top/v1")
        model = os.environ.get("OUROBOROS_WEBSEARCH_MODEL", "gpt-5")
        resp = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": f"Search the web and answer: {query}"}],
                "max_tokens": 4096,
            },
            timeout=60,
        )
        if resp.status_code != 200:
            return json.dumps({"error": f"HTTP {resp.status_code}: {resp.text[:300]}"})
        data = resp.json()
        choices = data.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or "(no answer)"
        return json.dumps({"answer": text}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": repr(e)}, ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("web_search", {
            "name": "web_search",
            "description": "Search the web. Returns JSON with answer.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
            }, "required": ["query"]},
        }, _web_search),
    ]
