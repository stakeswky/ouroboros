"""
Ouroboros — Tool execution engine.

Handles tool call parsing, execution (serial/parallel), caching,
timeout management, and result processing.
Extracted from loop.py to keep modules focused (Principle 5).
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolRegistry
from ouroboros.utils import utc_now_iso, append_jsonl, truncate_for_log, sanitize_tool_args_for_log, sanitize_tool_result_for_log

log = logging.getLogger(__name__)

# ── Tool classification sets ──────────────────────────────────────────

READ_ONLY_PARALLEL_TOOLS = frozenset({
    "repo_read", "repo_list",
    "drive_read", "drive_list",
    "web_search", "codebase_digest", "chat_history",
})

# Stateful browser tools require thread-affinity (Playwright sync uses greenlet)
STATEFUL_BROWSER_TOOLS = frozenset({"browse_page", "browser_action"})

# Tools safe to cache within a single task (read-only, deterministic)
CACHEABLE_TOOLS = READ_ONLY_PARALLEL_TOOLS | frozenset({
    "knowledge_read", "knowledge_list", "git_status", "git_diff",
    "list_github_issues", "get_github_issue",
    "analyze_screenshot",
})


# ── Helpers ───────────────────────────────────────────────────────────

def _safe_args(v: Any) -> Any:
    """Ensure value is JSON-serializable for logging."""
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


def truncate_tool_result(result: Any) -> str:
    """Hard-cap tool result string to 15000 characters."""
    result_str = str(result)
    if len(result_str) <= 15000:
        return result_str
    original_len = len(result_str)
    return result_str[:15000] + f"\n... (truncated from {original_len} chars)"


# ── Single tool execution ─────────────────────────────────────────────

def execute_single_tool(
    tools: ToolRegistry,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    task_id: str = "",
) -> Dict[str, Any]:
    """
    Execute a single tool call and return all needed info.

    Returns dict with: tool_call_id, fn_name, result, is_error, args_for_log, is_code_tool
    """
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]
    is_code_tool = fn_name in tools.CODE_TOOLS

    # Parse arguments
    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
    except (json.JSONDecodeError, ValueError) as e:
        result = f"⚠️ TOOL_ARG_ERROR: Could not parse arguments for '{fn_name}': {e}"
        return {
            "tool_call_id": tool_call_id,
            "fn_name": fn_name,
            "result": result,
            "is_error": True,
            "args_for_log": {},
            "is_code_tool": is_code_tool,
        }

    args_for_log = {k: _safe_args(v) for k, v in args.items()}

    # Execute
    try:
        result = tools.call(fn_name, args)
        result = truncate_tool_result(result)
        is_error = False
    except Exception as e:
        result = f"⚠️ TOOL_ERROR ({fn_name}): {e}"
        is_error = True

    # Log to tools.jsonl
    try:
        append_jsonl(drive_logs / "tools.jsonl", {
            "ts": utc_now_iso(),
            "task_id": task_id,
            "tool": fn_name,
            "args": sanitize_tool_args_for_log(fn_name, args_for_log),
            "result_preview": sanitize_tool_result_for_log(fn_name, truncate_for_log(result, 500)),
            "is_error": is_error,
        })
    except Exception:
        pass

    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": result,
        "is_error": is_error,
        "args_for_log": args_for_log,
        "is_code_tool": is_code_tool,
    }


# ── Stateful browser executor ────────────────────────────────────────

class StatefulToolExecutor:
    """
    Executes stateful browser tools on a dedicated thread to maintain
    Playwright's greenlet thread-affinity requirement.
    """

    def __init__(self, tools: ToolRegistry, drive_logs: pathlib.Path, task_id: str):
        self._tools = tools
        self._drive_logs = drive_logs
        self._task_id = task_id
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")

    def execute(self, tc: Dict[str, Any]) -> Dict[str, Any]:
        future = self._executor.submit(execute_single_tool, self._tools, tc, self._drive_logs, self._task_id)
        return future.result(timeout=120)

    def shutdown(self):
        self._executor.shutdown(wait=False, cancel_futures=True)


# ── Timeout wrapper ───────────────────────────────────────────────────

def _make_timeout_result(
    tc: Dict[str, Any],
    timeout_sec: int,
    tools: ToolRegistry,
) -> Dict[str, Any]:
    """Create a timeout error result for a tool call."""
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]
    is_code_tool = fn_name in tools.CODE_TOOLS

    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
    except (json.JSONDecodeError, ValueError):
        args = {}

    args_for_log = {k: _safe_args(v) for k, v in args.items()}

    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": f"⚠️ TOOL_TIMEOUT: '{fn_name}' exceeded {timeout_sec}s timeout. Try a simpler approach or different parameters.",
        "is_error": True,
        "args_for_log": args_for_log,
        "is_code_tool": is_code_tool,
    }


def execute_with_timeout(
    tools: ToolRegistry,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    timeout_sec: int,
    task_id: str = "",
    stateful_executor: Optional[StatefulToolExecutor] = None,
) -> Dict[str, Any]:
    """Execute a tool call with timeout protection."""
    fn_name = tc["function"]["name"]

    # Stateful browser tools use dedicated executor
    if fn_name in STATEFUL_BROWSER_TOOLS and stateful_executor:
        try:
            return stateful_executor.execute(tc)
        except Exception as e:
            return _make_timeout_result(tc, timeout_sec, tools)

    # Regular tools with thread-based timeout
    result_holder = [None]
    error_holder = [None]

    def _run():
        try:
            result_holder[0] = execute_single_tool(tools, tc, drive_logs, task_id)
        except Exception as e:
            error_holder[0] = e

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout_sec)

    if thread.is_alive():
        log.warning(f"Tool '{fn_name}' timed out after {timeout_sec}s")
        return _make_timeout_result(tc, timeout_sec, tools)

    if error_holder[0]:
        return _make_timeout_result(tc, timeout_sec, tools)

    return result_holder[0] or _make_timeout_result(tc, timeout_sec, tools)


# ── Result processing ─────────────────────────────────────────────────

def process_tool_results(
    results: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    llm_trace: Dict[str, Any],
    emit_progress: Optional[Callable] = None,
) -> Tuple[bool, bool]:
    """
    Process executed tool results: append to messages, update trace, emit progress.

    Returns:
        (any_code_tool, any_error) tuple
    """
    any_code_tool = False
    any_error = False

    for r in results:
        messages.append({
            "role": "tool",
            "tool_call_id": r["tool_call_id"],
            "content": r["result"],
        })

        if r["is_code_tool"]:
            any_code_tool = True
        if r["is_error"]:
            any_error = True

        # Update trace
        llm_trace.setdefault("tool_calls", []).append({
            "name": r["fn_name"],
            "args": r["args_for_log"],
            "result_preview": truncate_for_log(r["result"], 200),
            "is_error": r["is_error"],
        })

        # Emit progress for non-error results
        if emit_progress and not r["is_error"]:
            preview = truncate_for_log(r["result"], 100)
            if preview and r["fn_name"] not in ("chat_history",):
                emit_progress(f"{r['fn_name']}: {preview}")

    return any_code_tool, any_error


# ── Main orchestrator ─────────────────────────────────────────────────

def handle_tool_calls(
    tool_calls: List[Dict[str, Any]],
    tools: ToolRegistry,
    messages: List[Dict[str, Any]],
    drive_logs: pathlib.Path,
    llm_trace: Dict[str, Any],
    emit_progress: Optional[Callable] = None,
    task_id: str = "",
    tool_cache: Optional[Dict[str, str]] = None,
) -> Tuple[bool, bool]:
    """
    Execute tool calls (with caching, parallelism, timeouts) and process results.

    Returns:
        (any_code_tool, any_error) tuple
    """
    # Check cache first
    if tool_cache is not None:
        cached_results = []
        uncached_calls = []
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            if fn_name in CACHEABLE_TOOLS:
                try:
                    args_str = json.dumps(json.loads(tc["function"].get("arguments") or "{}"), sort_keys=True)
                    cache_key = f"{fn_name}:{args_str}"
                    if cache_key in tool_cache:
                        cached_results.append({
                            "tool_call_id": tc["id"],
                            "fn_name": fn_name,
                            "result": tool_cache[cache_key],
                            "is_error": False,
                            "args_for_log": {"_cached": True},
                            "is_code_tool": fn_name in tools.CODE_TOOLS,
                        })
                        log.info(f"Cache hit for {fn_name}")
                        continue
                except (json.JSONDecodeError, KeyError):
                    pass
            uncached_calls.append(tc)

        if cached_results:
            any_code, any_err = process_tool_results(cached_results, messages, llm_trace, emit_progress)
            if not uncached_calls:
                return any_code, any_err
            tool_calls = uncached_calls

    # Create stateful executor for browser tools if needed
    has_browser = any(tc["function"]["name"] in STATEFUL_BROWSER_TOOLS for tc in tool_calls)
    stateful_executor = StatefulToolExecutor(tools, drive_logs, task_id) if has_browser else None

    try:
        # Decide serial vs parallel execution
        all_read_only = all(tc["function"]["name"] in READ_ONLY_PARALLEL_TOOLS for tc in tool_calls)
        if len(tool_calls) == 1 or not all_read_only:
            # Serial execution
            results = [
                execute_with_timeout(
                    tools, tc, drive_logs,
                    tools.get_timeout(tc["function"]["name"]),
                    task_id, stateful_executor,
                )
                for tc in tool_calls
            ]
        else:
            # Parallel execution for read-only tools
            max_workers = min(len(tool_calls), 8)
            executor = ThreadPoolExecutor(max_workers=max_workers)
            try:
                future_to_index = {
                    executor.submit(
                        execute_with_timeout, tools, tc, drive_logs,
                        tools.get_timeout(tc["function"]["name"]), task_id,
                        stateful_executor,
                    ): idx
                    for idx, tc in enumerate(tool_calls)
                }
                results = [None] * len(tool_calls)
                for future in as_completed(future_to_index):
                    idx = future_to_index[future]
                    results[idx] = future.result()
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

        # Populate cache for cacheable tools
        if tool_cache is not None:
            for r in results:
                if not r["is_error"] and r["fn_name"] in CACHEABLE_TOOLS:
                    try:
                        tc_match = next(tc for tc in tool_calls if tc["id"] == r["tool_call_id"])
                        cache_key = f"{r['fn_name']}:{json.dumps(json.loads(tc_match['function'].get('arguments') or '{}'), sort_keys=True)}"
                        tool_cache[cache_key] = r["result"]
                    except (StopIteration, json.JSONDecodeError):
                        pass

        return process_tool_results(results, messages, llm_trace, emit_progress)

    finally:
        if stateful_executor:
            stateful_executor.shutdown()
