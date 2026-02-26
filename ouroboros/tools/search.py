"""Web search tool — DuckDuckGo-based, no API key required."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry


def _web_search(ctx: ToolContext, query: str) -> str:
    """Search the web using DuckDuckGo. Returns JSON with results."""
    try:
        from ddgs import DDGS
        results = DDGS().text(query, max_results=8)
        if not results:
            return json.dumps({"answer": "(no results)", "sources": []},
                              ensure_ascii=False)
        # Build a readable answer from snippets
        lines = []
        sources = []
        for r in results:
            title = r.get("title", "")
            body = r.get("body", "")
            href = r.get("href", "")
            lines.append(f"**{title}**\n{body}\n{href}")
            sources.append({"title": title, "url": href})
        answer = "\n\n".join(lines)
        return json.dumps({"answer": answer, "sources": sources},
                          ensure_ascii=False, indent=2)
    except ImportError:
        return json.dumps({"error": "ddgs not installed. Run: pip install ddgs"})
    except Exception as e:
        return json.dumps({"error": repr(e)}, ensure_ascii=False)


def _fetch_page(ctx: ToolContext, url: str, max_length: int = 5000) -> str:
    """Fetch a web page and return its content as text/markdown."""
    try:
        from ddgs import DDGS
        import httpx
        resp = httpx.get(url, follow_redirects=True, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        # Try to extract text content
        content_type = resp.headers.get("content-type", "")
        if "html" in content_type:
            try:
                from html.parser import HTMLParser
                # Simple text extraction
                class TextExtractor(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.text = []
                        self._skip = False
                    def handle_starttag(self, tag, attrs):
                        if tag in ("script", "style", "nav", "footer", "header"):
                            self._skip = True
                    def handle_endtag(self, tag):
                        if tag in ("script", "style", "nav", "footer", "header"):
                            self._skip = False
                    def handle_data(self, data):
                        if not self._skip:
                            stripped = data.strip()
                            if stripped:
                                self.text.append(stripped)
                extractor = TextExtractor()
                extractor.feed(resp.text)
                text = "\n".join(extractor.text)
            except Exception:
                text = resp.text
        else:
            text = resp.text
        if len(text) > max_length:
            text = text[:max_length] + f"\n\n... (truncated, {len(text)} total chars)"
        return json.dumps({"url": url, "content": text}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": repr(e), "url": url}, ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("web_search", {
            "name": "web_search",
            "description": "Search the web via DuckDuckGo. Returns JSON with answer + sources.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
            }, "required": ["query"]},
        }, _web_search),
        ToolEntry("fetch_page", {
            "name": "fetch_page",
            "description": "Fetch a web page and return its text content. Useful for reading articles, docs, etc.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "max_length": {"type": "integer", "description": "Max content length (default 5000)", "default": 5000},
            }, "required": ["url"]},
        }, _fetch_page),
    ]
