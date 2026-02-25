"""
Ouroboros — LLM client.

The only module that communicates with the LLM API.
Uses raw httpx instead of openai SDK to avoid Cloudflare fingerprint blocks.
Contract: chat(), default_model(), available_models(), add_usage().
"""

from __future__ import annotations

import logging
import os
import time
import json as _json
from typing import Any, Dict, List, Optional, Tuple

import httpx

log = logging.getLogger(__name__)

DEFAULT_LIGHT_MODEL = "google/gemini-3-pro-preview"

# Shared httpx client (lazy init)
_http: Optional[httpx.Client] = None

def _get_http() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=600, headers={"User-Agent": "Mozilla/5.0"})
    return _http


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
    """Fetch current pricing from API. Returns {model_id: (input_per_1m, cached_per_1m, output_per_1m)}."""
    try:
        resp = _get_http().get("https://oogg.top/v1/models", timeout=15,
                               headers={"Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}"})
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data", [])
        prefixes = ("anthropic/", "openai/", "google/", "meta-llama/", "x-ai/", "qwen/")
        pricing_dict = {}
        for model in models:
            model_id = model.get("id", "")
            if not model_id.startswith(prefixes):
                continue
            pricing = model.get("pricing", {})
            if not pricing or not pricing.get("prompt"):
                continue
            raw_prompt = float(pricing.get("prompt", 0))
            raw_completion = float(pricing.get("completion", 0))
            raw_cached_str = pricing.get("input_cache_read")
            raw_cached = float(raw_cached_str) if raw_cached_str else None
            input_per_1m = raw_prompt * 1_000_000
            output_per_1m = raw_completion * 1_000_000
            cached_per_1m = raw_cached * 1_000_000 if raw_cached is not None else input_per_1m * 0.1
            pricing_dict[model_id] = (input_per_1m, cached_per_1m, output_per_1m)
        return pricing_dict
    except Exception as e:
        log.warning(f"Failed to fetch pricing: {e}")
        return {}


class LLMClient:
    """LLM API wrapper using raw httpx. No openai SDK dependency."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://oogg.top/v1",
    ):
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._base_url = base_url.rstrip("/")

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "X-Title": "Ouroboros",
        }

    def _post(self, path: str, body: Dict[str, Any], timeout: float = 600) -> Dict[str, Any]:
        """POST to API and return parsed JSON. Raises on HTTP error."""
        url = f"{self._base_url}{path}"
        resp = _get_http().post(url, headers=self._headers(), json=body, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"LLM API error {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def _fetch_generation_cost(self, generation_id: str) -> Optional[float]:
        """Fetch cost from Generation API as fallback."""
        try:
            url = f"{self._base_url}/generation?id={generation_id}"
            resp = _get_http().get(url, headers=self._headers(), timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
            time.sleep(0.5)
            resp = _get_http().get(url, headers=self._headers(), timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
        except Exception:
            log.debug("Failed to fetch generation cost", exc_info=True)
        return None

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Single LLM call. Returns: (response_message_dict, usage_dict with cost)."""
        effort = normalize_reasoning_effort(reasoning_effort)

        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        if tools:
            tools_with_cache = list(tools)
            if tools_with_cache:
                last_tool = {**tools_with_cache[-1]}
                last_tool["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
                tools_with_cache[-1] = last_tool
            body["tools"] = tools_with_cache
            body["tool_choice"] = tool_choice

        resp_dict = self._post("/chat/completions", body)

        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        # Extract cached_tokens
        if not usage.get("cached_tokens"):
            prompt_details = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
                usage["cached_tokens"] = int(prompt_details["cached_tokens"])

        # Extract cache_write_tokens
        if not usage.get("cache_write_tokens"):
            pd = usage.get("prompt_tokens_details") or {}
            if isinstance(pd, dict):
                cw = (pd.get("cache_write_tokens")
                      or pd.get("cache_creation_tokens")
                      or pd.get("cache_creation_input_tokens"))
                if cw:
                    usage["cache_write_tokens"] = int(cw)

        # Ensure cost
        if not usage.get("cost"):
            gen_id = resp_dict.get("id") or ""
            if gen_id:
                cost = self._fetch_generation_cost(gen_id)
                if cost is not None:
                    usage["cost"] = cost

        return msg, usage

    def vision_query(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 1024,
        reasoning_effort: str = "low",
    ) -> Tuple[str, Dict[str, Any]]:
        """Send a vision query. Returns (text, usage)."""
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            if "url" in img:
                content.append({"type": "image_url", "image_url": {"url": img["url"]}})
            elif "base64" in img:
                mime = img.get("mime", "image/png")
                content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img['base64']}"}})
        messages = [{"role": "user", "content": content}]
        response_msg, usage = self.chat(
            messages=messages, model=model, tools=None,
            reasoning_effort=reasoning_effort, max_tokens=max_tokens,
        )
        return response_msg.get("content") or "", usage

    def default_model(self) -> str:
        return os.environ.get("OUROBOROS_MODEL", "claude-sonnet-4-6")

    def available_models(self) -> List[str]:
        main = os.environ.get("OUROBOROS_MODEL", "claude-sonnet-4-6")
        code = os.environ.get("OUROBOROS_MODEL_CODE", "")
        light = os.environ.get("OUROBOROS_MODEL_LIGHT", "")
        models = [main]
        if code and code != main:
            models.append(code)
        if light and light != main and light != code:
            models.append(light)
        return models
