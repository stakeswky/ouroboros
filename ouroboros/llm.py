"""
Ouroboros — LLM client.

The only module that communicates with the LLM API (OpenRouter).
Contract: chat(), default_model(), available_models(), add_usage().
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_LIGHT_MODEL = "google/gemini-3-pro-preview"


def normalize_reasoning_effort(value: str, default: str = "medium") -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def reasoning_rank(value: str) -> int:
    order = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
    return int(order.get(str(value or "").strip().lower(), 3))


def add_usage(total: Dict[str, Any], usage: Dict[str, Any]) -> None:
    """Accumulate usage from one LLM call into a running total."""
    for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_write_tokens"):
        total[k] = int(total.get(k) or 0) + int(usage.get(k) or 0)
    if usage.get("cost"):
        total["cost"] = float(total.get("cost") or 0) + float(usage["cost"])


def fetch_openrouter_pricing() -> Dict[str, Tuple[float, float, float]]:
    """
    Discover available models from the oogg.top proxy and validate static pricing coverage.

    oogg.top does not expose pricing data, so this function no longer returns live pricing.
    It returns the discovered model ID list (for discovery/validation), while callers should
    continue using the static pricing table for actual cost estimation.
    """
    try:
        import requests
    except ImportError:
        log.warning("requests not installed, cannot fetch model list")
        return {}

    try:
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            log.warning("OPENROUTER_API_KEY not set, cannot fetch model list from oogg.top")
            return {}

        url = "https://oogg.top/v1/models"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://colab.research.google.com/",
            "X-Title": "Ouroboros",
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()

        data = resp.json()
        models = data.get("data", [])
        model_ids = sorted(
            str(m.get("id", "")).strip()
            for m in models
            if isinstance(m, dict) and str(m.get("id", "")).strip()
        )

        try:
            from ouroboros.llm_runner import _MODEL_PRICING_STATIC
            missing = [mid for mid in model_ids if mid not in _MODEL_PRICING_STATIC]
            if missing:
                log.warning(
                    "oogg.top reports %d models missing from static pricing table: %s",
                    len(missing),
                    ", ".join(missing[:20]) + (" ..." if len(missing) > 20 else ""),
                )
            else:
                log.info("Static pricing table covers all %d discovered oogg.top models", len(model_ids))
        except Exception as cover_err:
            log.debug("Could not validate static pricing coverage: %s", cover_err)

        log.info("Discovered %d models from oogg.top/v1/models", len(model_ids))
        return model_ids  # type: ignore[return-value]

    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning(f"Failed to fetch model list from oogg.top: {e}")
        return {}


class LLMClient:
    """LLM API wrapper using requests. All LLM calls go through this class."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://oogg.top/v1",
    ):
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._base_url = base_url.rstrip("/")
        import requests as _req
        self._session = _req.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://colab.research.google.com/",
            "X-Title": "Ouroboros",
        })

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Single LLM call via requests. Returns: (response_message_dict, usage_dict)."""
        import json as _json

        effort = normalize_reasoning_effort(reasoning_effort)

        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice

        url = f"{self._base_url}/chat/completions"
        resp = self._session.post(url, json=body, timeout=300)
        resp.raise_for_status()
        resp_dict = resp.json()

        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        # Extract cached_tokens from prompt_tokens_details if available
        if not usage.get("cached_tokens"):
            prompt_details = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
                usage["cached_tokens"] = int(prompt_details["cached_tokens"])

        return msg, usage

    def vision_query(
        self,
        prompt: str,
        images: List[Dict[str, str]],
        model: str = "",
        reasoning_effort: str = "low",
        max_tokens: int = 4096,
    ) -> Tuple[str, Dict[str, Any]]:
        """Send a vision query with text + images."""
        if not model:
            model = os.environ.get("OUROBOROS_MODEL", "claude-opus-4-6")

        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            if "url" in img:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": img["url"]},
                })
            elif "base64" in img:
                mime = img.get("mime", "image/png")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{img['base64']}"},
                })

        messages = [{"role": "user", "content": content}]
        response_msg, usage = self.chat(
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
        )
        text = response_msg.get("content") or ""
        return text, usage

    def default_model(self) -> str:
        return os.environ.get("OUROBOROS_MODEL", "claude-opus-4-6")

    def available_models(self) -> List[str]:
        main = os.environ.get("OUROBOROS_MODEL", "claude-opus-4-6")
        code = os.environ.get("OUROBOROS_MODEL_CODE", "")
        light = os.environ.get("OUROBOROS_MODEL_LIGHT", "")
        models = [main]
        if code and code != main:
            models.append(code)
        if light and light != main and light != code:
            models.append(light)
        return models
