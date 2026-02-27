"""
Supervisor event dispatcher.

Maps event types from worker EVENT_Q to handler functions.
Extracted from colab_launcher.py main loop to keep it under 500 lines.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import time
import uuid
from typing import Any, Dict, Optional

# Lazy imports to avoid circular dependencies — everything comes through ctx

log = logging.getLogger(__name__)


def _handle_llm_usage(evt: Dict[str, Any], ctx: Any) -> None:
    usage = evt.get("usage") or {}

    # The calculated cost (possibly estimated) is in evt["cost"],
    # while usage["cost"] is the raw API value (often 0 from oogg.top).
    # Inject the calculated cost so update_budget_from_usage uses it.
    calculated_cost = evt.get("cost")
    if calculated_cost is not None:
        usage["cost"] = calculated_cost

    ctx.update_budget_from_usage(usage)

    # Log to events.jsonl for audit trail
    from ouroboros.utils import utc_now_iso, append_jsonl
    try:
        append_jsonl(ctx.DRIVE_ROOT / "logs" / "events.jsonl", {
            "ts": evt.get("ts", utc_now_iso()),
            "type": "llm_usage",
            "task_id": evt.get("task_id", ""),
            "category": evt.get("category", "other"),
            "model": evt.get("model", ""),
            "cost": calculated_cost or 0,
            "cost_estimated": evt.get("cost_estimated", False),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        })
    except Exception:
        log.warning("Failed to log llm_usage event to events.jsonl", exc_info=True)
        pass


def _handle_task_heartbeat(evt: Dict[str, Any], ctx: Any) -> None:
    task_id = str(evt.get("task_id") or "")
    if task_id and task_id in ctx.RUNNING:
        meta = ctx.RUNNING.get(task_id) or {}
        meta["last_heartbeat_at"] = time.time()
        phase = str(evt.get("phase") or "")
        if phase:
            meta["heartbeat_phase"] = phase
        ctx.RUNNING[task_id] = meta


def _handle_typing_start(evt: Dict[str, Any], ctx: Any) -> None:
    try:
        chat_id = int(evt.get("chat_id") or 0)
        if chat_id:
            ctx.TG.send_chat_action(chat_id, "typing")
    except Exception:
        log.debug("Failed to send typing action to chat", exc_info=True)
        pass


def _handle_send_message(evt: Dict[str, Any], ctx: Any) -> None:
    try:
        log_text = evt.get("log_text")
        fmt = str(evt.get("format") or "")
        is_progress = bool(evt.get("is_progress"))
        ctx.send_with_budget(
            int(evt["chat_id"]),
            str(evt.get("text") or ""),
            log_text=(str(log_text) if isinstance(log_text, str) else None),
            fmt=fmt,
            is_progress=is_progress,
        )
    except Exception as e:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "send_message_error",
                "error": repr(e),
            },
        )


def _handle_restart_request(evt: Dict[str, Any], ctx: Any) -> None:
    """Handle restart request from worker."""
    reason = str(evt.get("reason") or "agent requested restart")
    task_id = str(evt.get("task_id") or "")
    log.info(f"Restart requested by worker (task={task_id}): {reason}")

    # Check if the requesting task is an evolution task
    # If so, reset consecutive_failures before kill_workers() runs
    if task_id and task_id in ctx.RUNNING:
        meta = ctx.RUNNING.get(task_id) or {}
        if meta.get("type") == "evolution":
            try:
                from supervisor.state import acquire_file_lock, release_file_lock, \
                    _load_state_unlocked, _save_state_unlocked, STATE_LOCK_PATH
                lock_fd = acquire_file_lock(STATE_LOCK_PATH)
                try:
                    st = _load_state_unlocked()
                    if st.get("evolution_consecutive_failures", 0) > 0:
                        st["evolution_consecutive_failures"] = 0
                        _save_state_unlocked(st)
                        log.info("Reset evolution consecutive_failures (evolution task requesting restart)")
                finally:
                    release_file_lock(STATE_LOCK_PATH, lock_fd)
            except Exception:
                log.warning("Failed to reset evolution consecutive_failures", exc_info=True)

    ctx.append_jsonl(
        ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "restart_requested",
            "reason": reason,
            "task_id": task_id,
        },
    )
    ctx.RESTART_REQUESTED = True
    ctx.RESTART_REASON = reason


def _handle_promote_stable(evt: Dict[str, Any], ctx: Any) -> None:
    """Handle promote-to-stable request from worker."""
    reason = str(evt.get("reason") or "")
    log.info(f"Promote to stable requested: {reason}")
    try:
        from supervisor.git_ops import promote_to_stable
        result = promote_to_stable(ctx.REPO_DIR)
        if result.get("ok"):
            sha = result.get("sha", "unknown")
            ctx.send_with_budget(
                ctx.OWNER_CHAT_ID,
                f"✅ Promoted: ouroboros → ouroboros-stable ({sha[:8]})",
                is_progress=True,
            )
        else:
            error = result.get("error", "unknown error")
            ctx.send_with_budget(
                ctx.OWNER_CHAT_ID,
                f"❌ Failed to promote to stable: {error}",
                is_progress=True,
            )
    except Exception as e:
        log.error(f"Failed to promote to stable: {e}", exc_info=True)
        ctx.send_with_budget(
            ctx.OWNER_CHAT_ID,
            f"❌ Failed to promote to stable: {e}",
            is_progress=True,
        )


def _handle_task_done(evt: Dict[str, Any], ctx: Any) -> None:
    """Handle task completion event from worker."""
    task_id = str(evt.get("task_id") or "")
    if not task_id:
        return

    meta = ctx.RUNNING.pop(task_id, None)
    if meta is None:
        log.warning(f"task_done for unknown task {task_id}")
        return

    task_type = meta.get("type", "task")
    result_text = str(evt.get("result") or "")
    usage = evt.get("usage") or {}

    # Log completion
    ctx.append_jsonl(
        ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "task_done",
            "task_id": task_id,
            "task_type": task_type,
            "rounds": usage.get("rounds", 0),
        },
    )

    # Handle evolution task completion
    if task_type == "evolution":
        _handle_evolution_done(evt, ctx, meta, task_id, usage)

    # Store result for subtask retrieval
    if meta.get("parent_task_id"):
        ctx.TASK_RESULTS[task_id] = {
            "result": result_text,
            "usage": usage,
            "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }


def _handle_evolution_done(
    evt: Dict[str, Any],
    ctx: Any,
    meta: Dict[str, Any],
    task_id: str,
    usage: Dict[str, Any],
) -> None:
    """Handle evolution task completion — update state, check success."""
    from supervisor.state import acquire_file_lock, release_file_lock, \
        _load_state_unlocked, _save_state_unlocked, STATE_LOCK_PATH

    rounds = usage.get("rounds", 0)
    # Success = at least 3 rounds (API failures give 0, code failures give 1-2)
    success = rounds >= 3

    lock_fd = acquire_file_lock(STATE_LOCK_PATH)
    try:
        st = _load_state_unlocked()
        if success:
            st["evolution_consecutive_failures"] = 0
            st["evolution_cycle"] = int(st.get("evolution_cycle") or 0) + 1
        else:
            # Don't count API failures (rounds==0) as evolution failures
            if rounds > 0:
                st["evolution_consecutive_failures"] = int(
                    st.get("evolution_consecutive_failures") or 0
                ) + 1
            # else: API failure, don't increment
        st["last_evolution_task_at"] = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat()
        _save_state_unlocked(st)

        failures = st.get("evolution_consecutive_failures", 0)
    finally:
        release_file_lock(STATE_LOCK_PATH, lock_fd)

    if not success and rounds > 0 and failures >= 3:
        ctx.send_with_budget(
            ctx.OWNER_CHAT_ID,
            "🧬⚠️ Evolution paused: 3 consecutive failures. "
            "Use /evolve start to resume after investigating the issue.",
            is_progress=True,
        )
        # Disable evolution mode
        lock_fd = acquire_file_lock(STATE_LOCK_PATH)
        try:
            st = _load_state_unlocked()
            st["evolution_mode_enabled"] = False
            _save_state_unlocked(st)
        finally:
            release_file_lock(STATE_LOCK_PATH, lock_fd)


def _handle_send_photo(evt: Dict[str, Any], ctx: Any) -> None:
    """Handle send_photo event from worker."""
    try:
        import base64
        chat_id = int(evt.get("chat_id") or 0)
        photo_b64 = evt.get("photo_b64", "")
        caption = str(evt.get("caption") or "")
        if chat_id and photo_b64:
            photo_bytes = base64.b64decode(photo_b64)
            ctx.TG.send_photo(chat_id, photo_bytes, caption=caption[:1024])
    except Exception as e:
        log.warning(f"Failed to send photo: {e}", exc_info=True)


# ── Event handler registry ──────────────────────────────────────────
EVENT_HANDLERS = {
    "llm_usage": _handle_llm_usage,
    "task_heartbeat": _handle_task_heartbeat,
    "typing_start": _handle_typing_start,
    "send_message": _handle_send_message,
    "restart_request": _handle_restart_request,
    "promote_stable": _handle_promote_stable,
    "task_done": _handle_task_done,
    "send_photo": _handle_send_photo,
}


def dispatch_worker_event(evt: Any, ctx: Any) -> None:
    """Route a single worker event to its handler."""
    if not isinstance(evt, dict):
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "invalid_worker_event",
                "error": f"expected dict, got {type(evt).__name__}",
                "event_repr": repr(evt)[:1000],
            },
        )
        return

    event_type = str(evt.get("type") or "").strip()
    if not event_type:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "invalid_worker_event",
                "error": "missing event.type",
                "event_repr": repr(evt)[:1000],
            },
        )
        return

    handler = EVENT_HANDLERS.get(event_type)
    if handler is None:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "unknown_worker_event",
                "event_type": event_type,
                "event_repr": repr(evt)[:1000],
            },
        )
        return

    try:
        handler(evt, ctx)
    except Exception as e:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "worker_event_handler_error",
                "event_type": event_type,
                "error": repr(e),
            },
        )
