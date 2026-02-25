"""
Ouroboros context builder.

Assembles LLM context from prompts, memory, logs, and runtime state.
Extracted from agent.py to keep the agent thin and focused.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.compaction import (
    apply_message_token_soft_cap,
    compact_tool_history,
    compact_tool_history_llm,
)
from ouroboros.utils import (
    utc_now_iso, read_text, clip_text, get_git_info,
)
from ouroboros.memory import Memory

log = logging.getLogger(__name__)


def _build_user_content(task: Dict[str, Any]) -> Any:
    """Build user message content. Supports text + optional image."""
    text = task.get("text", "")
    image_b64 = task.get("image_base64")
    image_mime = task.get("image_mime", "image/jpeg")
    image_caption = task.get("image_caption", "")

    if not image_b64:
        # Return fallback text if both text and image are empty
        if not text:
            return "(empty message)"
        return text

    # Multipart content with text + image
    parts = []
    # Combine caption and text for the text part
    combined_text = ""
    if image_caption:
        combined_text = image_caption
    if text and text != image_caption:
        combined_text = (combined_text + "\n" + text).strip() if combined_text else text

    # Always include a text part when there's an image
    if not combined_text:
        combined_text = "Analyze the screenshot"

    parts.append({"type": "text", "text": combined_text})
    parts.append({
        "type": "image_url",
        "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}
    })
    return parts


def _build_runtime_section(env: Any, task: Dict[str, Any]) -> str:
    """Build the runtime context section (utc_now, repo_dir, drive_root, git_head, git_branch, task info, budget info)."""
    # --- Git context ---
    try:
        git_branch, git_sha = get_git_info(env.repo_dir)
    except Exception:
        log.debug("Failed to get git info for context", exc_info=True)
        git_branch, git_sha = "unknown", "unknown"

    # --- Budget calculation ---
    budget_info = None
    try:
        state_json = _safe_read(env.drive_path("state/state.json"), fallback="{}")
        state_data = json.loads(state_json)
        spent_usd = float(state_data.get("spent_usd", 0))
        total_usd = float(os.environ.get("TOTAL_BUDGET", "1"))
        remaining_usd = total_usd - spent_usd
        budget_info = {"total_usd": total_usd, "spent_usd": spent_usd, "remaining_usd": remaining_usd}
    except Exception:
        log.debug("Failed to calculate budget info for context", exc_info=True)
        pass

    # --- Runtime context JSON ---
    runtime_data = {
        "utc_now": utc_now_iso(),
        "repo_dir": str(env.repo_dir),
        "drive_root": str(env.drive_root),
        "git_head": git_sha,
        "git_branch": git_branch,
        "task": {"id": task.get("id"), "type": task.get("type")},
    }
    if budget_info:
        runtime_data["budget"] = budget_info
    runtime_ctx = json.dumps(runtime_data, ensure_ascii=False, indent=2)
    return "## Runtime context\n\n" + runtime_ctx


def _build_memory_sections(memory: Memory) -> List[str]:
    """Build scratchpad, identity, dialogue summary sections."""
    sections = []

    scratchpad_raw = memory.load_scratchpad()
    sections.append("## Scratchpad\n\n" + clip_text(scratchpad_raw, 90000))

    identity_raw = memory.load_identity()
    sections.append("## Identity\n\n" + clip_text(identity_raw, 80000))

    # Dialogue summary (key moments from chat history)
    summary_path = memory.drive_root / "memory" / "dialogue_summary.md"
    if summary_path.exists():
        summary_text = read_text(summary_path)
        if summary_text.strip():
            sections.append("## Dialogue Summary\n\n" + clip_text(summary_text, 20000))

    return sections


def _build_recent_sections(memory: Memory, env: Any, task_id: str = "") -> List[str]:
    """Build recent chat, recent progress, recent tools, recent events sections."""
    sections = []

    chat_summary = memory.summarize_chat(
        memory.read_jsonl_tail("chat.jsonl", 200))
    if chat_summary:
        sections.append("## Recent chat\n\n" + chat_summary)

    progress_entries = memory.read_jsonl_tail("progress.jsonl", 200)
    if task_id:
        progress_entries = [e for e in progress_entries if e.get("task_id") == task_id]
    progress_summary = memory.summarize_progress(progress_entries, limit=15)
    if progress_summary:
        sections.append("## Recent progress\n\n" + progress_summary)

    tools_entries = memory.read_jsonl_tail("tools.jsonl", 200)
    if task_id:
        tools_entries = [e for e in tools_entries if e.get("task_id") == task_id]
    tools_summary = memory.summarize_tools(tools_entries)
    if tools_summary:
        sections.append("## Recent tools\n\n" + tools_summary)

    events_entries = memory.read_jsonl_tail("events.jsonl", 200)
    if task_id:
        events_entries = [e for e in events_entries if e.get("task_id") == task_id]
    events_summary = memory.summarize_events(events_entries)
    if events_summary:
        sections.append("## Recent events\n\n" + events_summary)

    supervisor_summary = memory.summarize_supervisor(
        memory.read_jsonl_tail("supervisor.jsonl", 200))
    if supervisor_summary:
        sections.append("## Supervisor\n\n" + supervisor_summary)

    return sections


def _build_health_invariants(env: Any) -> str:
    from ouroboros.health import build_health_invariants
    return build_health_invariants(env)


def build_llm_messages(
    env: Any,
    memory: Memory,
    task: Dict[str, Any],
    review_context_builder: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Build the full LLM message context for a task.

    Args:
        env: Env instance with repo_path/drive_path helpers
        memory: Memory instance for scratchpad/identity/logs
        task: Task dict with id, type, text, etc.
        review_context_builder: Optional callable for review tasks (signature: () -> str)

    Returns:
        (messages, cap_info) tuple:
            - messages: List of message dicts ready for LLM
            - cap_info: Dict with token trimming metadata
    """
    # --- Extract task type for adaptive context ---
    task_type = str(task.get("type") or "user")

    # --- Read base prompts and state ---
    base_prompt = _safe_read(
        env.repo_path("prompts/SYSTEM.md"),
        fallback="You are Ouroboros. Your base prompt could not be loaded."
    )
    bible_md = _safe_read(env.repo_path("BIBLE.md"))
    readme_md = _safe_read(env.repo_path("README.md"))
    state_json = _safe_read(env.drive_path("state/state.json"), fallback="{}")

    # --- Load memory ---
    memory.ensure_files()

    # --- Assemble messages with 3-block prompt caching ---
    # Block 1: Static content (SYSTEM.md + BIBLE.md + README) — cached
    # Block 2: Semi-stable content (identity + scratchpad + knowledge) — cached
    # Block 3: Dynamic content (state + runtime + recent logs) — uncached

    # BIBLE.md always included (Constitution requires it for every decision)
    # README.md only for evolution/review (architecture context)
    needs_full_context = task_type in ("evolution", "review", "scheduled")
    static_text = (
        base_prompt + "\n\n"
        + "## BIBLE.md\n\n" + clip_text(bible_md, 180000)
    )
    if needs_full_context:
        static_text += "\n\n## README.md\n\n" + clip_text(readme_md, 180000)

    # Semi-stable content: identity, scratchpad, knowledge
    # These change ~once per task, not per round
    semi_stable_parts = []
    semi_stable_parts.extend(_build_memory_sections(memory))

    kb_index_path = env.drive_path("memory/knowledge/_index.md")
    if kb_index_path.exists():
        kb_index = kb_index_path.read_text(encoding="utf-8")
        if kb_index.strip():
            semi_stable_parts.append("## Knowledge base\n\n" + clip_text(kb_index, 50000))

    semi_stable_text = "\n\n".join(semi_stable_parts)

    # Dynamic content: changes every round
    dynamic_parts = [
        "## Drive state\n\n" + clip_text(state_json, 90000),
        _build_runtime_section(env, task),
    ]

    # Health invariants — surfaces anomalies for LLM-first self-detection (Bible P0+P3)
    health_section = _build_health_invariants(env)
    if health_section:
        dynamic_parts.append(health_section)

    dynamic_parts.extend(_build_recent_sections(memory, env, task_id=task.get("id", "")))

    if str(task.get("type") or "") == "review" and review_context_builder is not None:
        try:
            review_ctx = review_context_builder()
            if review_ctx:
                dynamic_parts.append(review_ctx)
        except Exception:
            log.debug("Failed to build review context", exc_info=True)
            pass

    dynamic_text = "\n\n".join(dynamic_parts)

    # System message with 3 content blocks for optimal caching
    messages: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": static_text,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                },
                {
                    "type": "text",
                    "text": semi_stable_text,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": dynamic_text,
                },
            ],
        },
        {"role": "user", "content": _build_user_content(task)},
    ]

    # --- Soft-cap token trimming ---
    messages, cap_info = apply_message_token_soft_cap(messages, 200000)

    return messages, cap_info


def _safe_read(path: pathlib.Path, fallback: str = "") -> str:
    """Read a file, returning fallback if it doesn't exist or errors."""
    try:
        if path.exists():
            return read_text(path)
    except Exception:
        log.debug(f"Failed to read file {path} in _safe_read", exc_info=True)
        pass
    return fallback
