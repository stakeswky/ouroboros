"""
Ouroboros — LLM tool loop.

Core loop: send messages to LLM, execute tool calls, repeat until final response.
Extracted from agent.py to keep the agent thin.
"""

from __future__ import annotations

import os
import pathlib
import queue
from typing import Any, Callable, Dict, List, Optional, Tuple

import logging

from ouroboros.llm import LLMClient, normalize_reasoning_effort
from ouroboros.llm_runner import _get_pricing, _estimate_cost, _emit_llm_usage_event, _call_llm_with_retry
from ouroboros.tools.registry import ToolRegistry
from ouroboros.compaction import compact_tool_history, compact_tool_history_llm
from ouroboros.tool_executor import _handle_tool_calls, _StatefulToolExecutor, READ_ONLY_PARALLEL_TOOLS, STATEFUL_BROWSER_TOOLS, CACHEABLE_TOOLS
from ouroboros.utils import estimate_tokens

log = logging.getLogger(__name__)

def _handle_text_response(
    content: Optional[str],
    llm_trace: Dict[str, Any],
    accumulated_usage: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Handle LLM response without tool calls (final response).

    Returns: (final_text, accumulated_usage, llm_trace)
    """
    if content and content.strip():
        llm_trace["assistant_notes"].append(content.strip()[:320])
    return (content or ""), accumulated_usage, llm_trace


def _check_budget_limits(
    budget_remaining_usd: Optional[float],
    accumulated_usage: Dict[str, Any],
    round_idx: int,
    messages: List[Dict[str, Any]],
    llm: LLMClient,
    active_model: str,
    active_effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    event_queue: Optional[queue.Queue],
    llm_trace: Dict[str, Any],
    task_type: str = "task",
) -> Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """
    Check budget limits and handle budget overrun.

    Returns:
        None if budget is OK (continue loop)
        (final_text, accumulated_usage, llm_trace) if budget exceeded (stop loop)
    """
    if budget_remaining_usd is None:
        return None

    task_cost = accumulated_usage.get("cost", 0)
    budget_pct = task_cost / budget_remaining_usd if budget_remaining_usd > 0 else 1.0

    if budget_pct > 0.5:
        # Hard stop — protect the budget
        finish_reason = f"Task spent ${task_cost:.3f} (>50% of remaining ${budget_remaining_usd:.2f}). Budget exhausted."
        messages.append({"role": "system", "content": f"[BUDGET LIMIT] {finish_reason} Give your final response now."})
        try:
            final_msg, final_cost = _call_llm_with_retry(
                llm, messages, active_model, None, active_effort,
                max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type
            )
            if final_msg:
                return (final_msg.get("content") or finish_reason), accumulated_usage, llm_trace
            return finish_reason, accumulated_usage, llm_trace
        except Exception:
            log.warning("Failed to get final response after budget limit", exc_info=True)
            return finish_reason, accumulated_usage, llm_trace
    elif budget_pct > 0.3 and round_idx % 10 == 0:
        # Soft nudge every 10 rounds when spending is significant
        messages.append({"role": "system", "content": f"[INFO] Task spent ${task_cost:.3f} of ${budget_remaining_usd:.2f}. Wrap up if possible."})

    return None


def _maybe_inject_self_check(
    round_idx: int,
    max_rounds: int,
    messages: List[Dict[str, Any]],
    accumulated_usage: Dict[str, Any],
    emit_progress: Callable[[str], None],
) -> None:
    """Inject a soft self-check reminder every REMINDER_INTERVAL rounds.

    This is a cognitive feature (Bible P0: subjectivity) — the agent reflects
    on its own resource usage and strategy, not a hard kill.
    """
    REMINDER_INTERVAL = 50
    if round_idx <= 1 or round_idx % REMINDER_INTERVAL != 0:
        return
    ctx_tokens = sum(
        estimate_tokens(str(m.get("content", "")))
        if isinstance(m.get("content"), str)
        else sum(estimate_tokens(str(b.get("text", ""))) for b in m.get("content", []) if isinstance(b, dict))
        for m in messages
    )
    task_cost = accumulated_usage.get("cost", 0)
    checkpoint_num = round_idx // REMINDER_INTERVAL

    reminder = (
        f"[CHECKPOINT {checkpoint_num} — round {round_idx}/{max_rounds}]\n"
        f"📊 Context: ~{ctx_tokens} tokens | Cost so far: ${task_cost:.2f} | "
        f"Rounds remaining: {max_rounds - round_idx}\n\n"
        f"⏸️ PAUSE AND REFLECT before continuing:\n"
        f"1. Am I making real progress, or repeating the same actions?\n"
        f"2. Is my current strategy working? Should I try something different?\n"
        f"3. Is my context bloated with old tool results I no longer need?\n"
        f"   → If yes, call `compact_context` to summarize them selectively.\n"
        f"4. Have I been stuck on the same sub-problem for many rounds?\n"
        f"   → If yes, consider: simplify the approach, skip the sub-problem, or finish with what I have.\n"
        f"5. Should I just STOP and return my best result so far?\n\n"
        f"This is not a hard limit — you decide. But be honest with yourself."
    )
    messages.append({"role": "system", "content": reminder})
    emit_progress(f"🔄 Checkpoint {checkpoint_num} at round {round_idx}: ~{ctx_tokens} tokens, ${task_cost:.2f} spent")


def _setup_dynamic_tools(tools_registry, tool_schemas, messages):
    """
    Wire tool-discovery handlers onto an existing tool_schemas list.

    Creates closures for list_available_tools / enable_tools, registers them
    as handler overrides, and injects a system message advertising non-core
    tools.  Mutates tool_schemas in-place (via list.append) when tools are
    enabled, so the caller's reference stays live.

    Returns (tool_schemas, enabled_extra_set).
    """
    enabled_extra: set = set()

    def _handle_list_tools(ctx=None, **kwargs):
        non_core = tools_registry.list_non_core_tools()
        if not non_core:
            return "All tools are already in your active set."
        lines = [f"**{len(non_core)} additional tools available** (use `enable_tools` to activate):\n"]
        for t in non_core:
            lines.append(f"- **{t['name']}**: {t['description'][:120]}")
        return "\n".join(lines)

    def _handle_enable_tools(ctx=None, tools: str = "", **kwargs):
        names = [n.strip() for n in tools.split(",") if n.strip()]
        enabled, not_found = [], []
        for name in names:
            schema = tools_registry.get_schema_by_name(name)
            if schema and name not in enabled_extra:
                tool_schemas.append(schema)
                enabled_extra.add(name)
                enabled.append(name)
            elif name in enabled_extra:
                enabled.append(f"{name} (already active)")
            else:
                not_found.append(name)
        parts = []
        if enabled:
            parts.append(f"✅ Enabled: {', '.join(enabled)}")
        if not_found:
            parts.append(f"❌ Not found: {', '.join(not_found)}")
        return "\n".join(parts) if parts else "No tools specified."

    tools_registry.override_handler("list_available_tools", _handle_list_tools)
    tools_registry.override_handler("enable_tools", _handle_enable_tools)

    non_core_count = len(tools_registry.list_non_core_tools())
    if non_core_count > 0:
        messages.append({
            "role": "system",
            "content": (
                f"Note: You have {len(tool_schemas)} core tools loaded. "
                f"There are {non_core_count} additional tools available "
                f"(use `list_available_tools` to see them, `enable_tools` to activate). "
                f"Core tools cover most tasks. Enable extras only when needed."
            ),
        })

    return tool_schemas, enabled_extra


def _drain_incoming_messages(
    messages: List[Dict[str, Any]],
    incoming_messages: queue.Queue,
    drive_root: Optional[pathlib.Path],
    task_id: str,
    event_queue: Optional[queue.Queue],
    _owner_msg_seen: set,
) -> None:
    """
    Inject owner messages received during task execution.
    Drains both the in-process queue and the Drive mailbox.
    """
    # Inject owner messages received during task execution
    while not incoming_messages.empty():
        try:
            injected = incoming_messages.get_nowait()
            messages.append({"role": "user", "content": injected})
        except queue.Empty:
            break

    # Drain per-task owner messages from Drive mailbox (written by forward_to_worker tool)
    if drive_root is not None and task_id:
        from ouroboros.owner_inject import drain_owner_messages
        drive_msgs = drain_owner_messages(drive_root, task_id=task_id, seen_ids=_owner_msg_seen)
        for dmsg in drive_msgs:
            messages.append({
                "role": "user",
                "content": f"[Owner message during task]: {dmsg}",
            })
            # Log for duplicate processing detection (health invariant #5)
            if event_queue is not None:
                try:
                    event_queue.put_nowait({
                        "type": "owner_message_injected",
                        "task_id": task_id,
                        "text": dmsg[:200],
                    })
                except Exception:
                    pass


def _run_one_round(
    round_idx: int,
    MAX_ROUNDS: int,
    messages: List[Dict[str, Any]],
    tools: "ToolRegistry",
    llm: "LLMClient",
    tool_schemas: List[Dict[str, Any]],
    active_model: str,
    active_effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    event_queue: Optional[queue.Queue],
    accumulated_usage: Dict[str, Any],
    task_type: str,
    llm_trace: Dict[str, Any],
    emit_progress: Callable[[str], None],
    incoming_messages: queue.Queue,
    drive_root: Optional[pathlib.Path],
    budget_remaining_usd: Optional[float],
    _owner_msg_seen: set,
    stateful_executor: "_StatefulToolExecutor",
    tool_cache: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], str, str]:
    if round_idx > MAX_ROUNDS:
        finish_reason = f"⚠️ Task exceeded MAX_ROUNDS ({MAX_ROUNDS}). Consider decomposing into subtasks via schedule_task."
        messages.append({"role": "system", "content": f"[ROUND_LIMIT] {finish_reason}"})
        try:
            final_msg, final_cost = _call_llm_with_retry(
                llm, messages, active_model, None, active_effort,
                max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type
            )
            if final_msg:
                return (final_msg.get("content") or finish_reason), active_model, active_effort
            return finish_reason, active_model, active_effort
        except Exception:
            log.warning("Failed to get final response after round limit", exc_info=True)
            return finish_reason, active_model, active_effort

    _maybe_inject_self_check(round_idx, MAX_ROUNDS, messages, accumulated_usage, emit_progress)

    ctx = tools._ctx
    if ctx.active_model_override:
        active_model = ctx.active_model_override
        ctx.active_model_override = None
    if ctx.active_effort_override:
        active_effort = normalize_reasoning_effort(ctx.active_effort_override, default=active_effort)
        ctx.active_effort_override = None

    _drain_incoming_messages(messages, incoming_messages, drive_root, task_id, event_queue, _owner_msg_seen)

    pending_compaction = getattr(tools._ctx, '_pending_compaction', None)
    if pending_compaction is not None:
        messages[:] = compact_tool_history_llm(messages, keep_recent=pending_compaction)
        tools._ctx._pending_compaction = None
    elif round_idx > 8:
        messages[:] = compact_tool_history(messages, keep_recent=6)
    elif round_idx > 3:
        if len(messages) > 60:
            messages[:] = compact_tool_history(messages, keep_recent=6)

    msg, cost = _call_llm_with_retry(
        llm, messages, active_model, tool_schemas, active_effort,
        max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type
    )

    if msg is None:
        fallback_list_raw = os.environ.get(
            "OUROBOROS_MODEL_FALLBACK_LIST",
            "google/gemini-2.5-pro-preview,openai/o3,anthropic/claude-sonnet-4.6"
        )
        fallback_candidates = [m.strip() for m in fallback_list_raw.split(",") if m.strip()]
        fallback_model = None
        for candidate in fallback_candidates:
            if candidate != active_model:
                fallback_model = candidate
                break
        if fallback_model is None:
            return (
                f"⚠️ Failed to get a response from model {active_model} after {max_retries} attempts. "
                f"All fallback models match the active one. Try rephrasing your request."
            ), active_model, active_effort

        fallback_progress = f"⚡ Fallback: {active_model} → {fallback_model} after empty response"
        emit_progress(fallback_progress)

        msg, fallback_cost = _call_llm_with_retry(
            llm, messages, fallback_model, tool_schemas, active_effort,
            max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type
        )

        if msg is None:
            return (
                f"⚠️ Failed to get a response from the model after {max_retries} attempts. "
                f"Fallback model ({fallback_model}) also returned no response."
            ), active_model, active_effort

    tool_calls = msg.get("tool_calls") or []
    content = msg.get("content")
    if not tool_calls:
        final_text, _usage, _trace = _handle_text_response(content, llm_trace, accumulated_usage)
        return final_text, active_model, active_effort

    messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})

    if content and content.strip():
        emit_progress(content.strip())
        llm_trace["assistant_notes"].append(content.strip()[:320])

    error_count = _handle_tool_calls(
        tool_calls, tools, drive_logs, task_id, stateful_executor,
        messages, llm_trace, emit_progress, tool_cache=tool_cache
    )

    budget_result = _check_budget_limits(
        budget_remaining_usd, accumulated_usage, round_idx, messages,
        llm, active_model, active_effort, max_retries, drive_logs,
        task_id, event_queue, llm_trace, task_type
    )
    if budget_result is not None:
        final_text, _usage, _trace = budget_result
        return final_text, active_model, active_effort

    return None, active_model, active_effort


def run_llm_loop(
    messages: List[Dict[str, Any]],
    tools: ToolRegistry,
    llm: LLMClient,
    drive_logs: pathlib.Path,
    emit_progress: Callable[[str], None],
    incoming_messages: queue.Queue,
    task_type: str = "",
    task_id: str = "",
    budget_remaining_usd: Optional[float] = None,
    event_queue: Optional[queue.Queue] = None,
    initial_effort: str = "medium",
    drive_root: Optional[pathlib.Path] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Core LLM-with-tools loop.

    Sends messages to LLM, executes tool calls, retries on errors.
    LLM controls model/effort via switch_model tool (LLM-first, Bible P3).

    Args:
        budget_remaining_usd: If set, forces completion when task cost exceeds 50% of this budget
        initial_effort: Initial reasoning effort level (default "medium")

    Returns: (final_text, accumulated_usage, llm_trace)
    """
    # LLM-first: single default model, LLM switches via tool if needed
    active_model = llm.default_model()
    active_effort = initial_effort

    llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}
    accumulated_usage: Dict[str, Any] = {}
    max_retries = 10
    # Wire module-level registry ref so tool_discovery handlers work outside run_llm_loop too
    from ouroboros.tools import tool_discovery as _td
    _td.set_registry(tools)

    # Selective tool schemas: core set + meta-tools for discovery.
    tool_schemas = tools.schemas(core_only=True)
    tool_schemas, _enabled_extra_tools = _setup_dynamic_tools(tools, tool_schemas, messages)

    # Set budget tracking on tool context for real-time usage events
    tools._ctx.event_queue = event_queue
    tools._ctx.task_id = task_id
    # Thread-sticky executor for browser tools (Playwright sync requires greenlet thread-affinity)
    stateful_executor = _StatefulToolExecutor()
    # Dedup set for per-task owner messages from Drive mailbox
    _owner_msg_seen: set = set()
    # Cache for read-only tool calls to avoid duplicate execution
    tool_cache: Dict[str, str] = {}
    try:
        MAX_ROUNDS = max(1, int(os.environ.get("OUROBOROS_MAX_ROUNDS", "200")))
    except (ValueError, TypeError):
        MAX_ROUNDS = 200
        log.warning("Invalid OUROBOROS_MAX_ROUNDS, defaulting to 200")
    round_idx = 0
    try:
        while True:
            round_idx += 1
            final_text, active_model, active_effort = _run_one_round(
                round_idx, MAX_ROUNDS, messages, tools, llm, tool_schemas,
                active_model, active_effort, max_retries, drive_logs, task_id,
                event_queue, accumulated_usage, task_type, llm_trace, emit_progress,
                incoming_messages, drive_root, budget_remaining_usd, _owner_msg_seen,
                stateful_executor, tool_cache=tool_cache,
            )
            if final_text is not None:
                return final_text, accumulated_usage, llm_trace

    finally:
        # Cleanup thread-sticky executor for stateful tools
        if stateful_executor:
            try:
                stateful_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                log.warning("Failed to shutdown stateful executor", exc_info=True)
        # Cleanup per-task mailbox
        if drive_root is not None and task_id:
            try:
                from ouroboros.owner_inject import cleanup_task_mailbox
                cleanup_task_mailbox(drive_root, task_id)
            except Exception:
                log.debug("Failed to cleanup task mailbox", exc_info=True)
