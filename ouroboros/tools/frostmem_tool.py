"""Frostmem — semantic memory (remember/recall/forget) via remote mem0 daemon."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import httpx

from ouroboros.tools.registry import ToolContext, ToolEntry

_FROSTMEM_URL = os.environ.get("FROSTMEM_URL", "http://100.93.72.102:18790")
_USER = "ouroboros"
_TIMEOUT = 30


def _remember(ctx: ToolContext, content: str, metadata: str = "{}") -> str:
    """Store a memory."""
    try:
        meta = json.loads(metadata) if metadata else {}
    except json.JSONDecodeError:
        meta = {"raw": metadata}
    try:
        resp = httpx.post(f"{_FROSTMEM_URL}/remember", json={
            "content": content, "user_id": _USER, "metadata": meta,
        }, timeout=_TIMEOUT)
        return resp.text
    except Exception as e:
        return json.dumps({"error": repr(e)})


def _recall(ctx: ToolContext, query: str, limit: int = 5) -> str:
    """Search memories semantically."""
    try:
        resp = httpx.post(f"{_FROSTMEM_URL}/recall", json={
            "query": query, "user_id": _USER, "limit": limit,
        }, timeout=_TIMEOUT)
        return resp.text
    except Exception as e:
        return json.dumps({"error": repr(e)})


def _forget(ctx: ToolContext, memory_id: str) -> str:
    """Delete a specific memory by ID."""
    try:
        resp = httpx.request("DELETE", f"{_FROSTMEM_URL}/forget", json={
            "memory_id": memory_id, "user_id": _USER,
        }, timeout=_TIMEOUT)
        return resp.text
    except Exception as e:
        return json.dumps({"error": repr(e)})


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("frostmem_remember", {
            "name": "frostmem_remember",
            "description": "Store a semantic memory (survives restarts). Use for decisions, lessons learned, architecture insights, evolution history. Metadata: {category, importance, tags}.",
            "parameters": {"type": "object", "properties": {
                "content": {"type": "string", "description": "The memory content to store"},
                "metadata": {"type": "string", "description": "JSON metadata: {category, importance, tags}"},
            }, "required": ["content"]},
        }, _remember),
        ToolEntry("frostmem_recall", {
            "name": "frostmem_recall",
            "description": "Semantically search memories. Use before making decisions to recall past lessons, architecture choices, failed approaches.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Semantic search query"},
                "limit": {"type": "integer", "description": "Max results (default 5)"},
            }, "required": ["query"]},
        }, _recall),
        ToolEntry("frostmem_forget", {
            "name": "frostmem_forget",
            "description": "Delete a specific memory by ID.",
            "parameters": {"type": "object", "properties": {
                "memory_id": {"type": "string"},
            }, "required": ["memory_id"]},
        }, _forget),
    ]
