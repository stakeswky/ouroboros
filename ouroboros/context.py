"""Ouroboros context builder."""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.compaction import apply_message_token_soft_cap
from ouroboros.utils import (
    utc_now_iso, read_text, clip_text, get_git_info, estimate_tokens,
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
        if not text:
            return "(empty message)"
        return text
    parts = []
    combined_text = ""
    if image_caption:
        combined_text = image_caption
    if text and text != image_caption:
        combined_text = (combined_text + "\n" + text).strip() if combined_text else text

    if not combined_text:
        combined_text = "Analyze the screenshot"
    parts.append({"type": "text", "text": combined_text})
    parts.append({
        "type": "image_url",
        "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}
    })
    return parts


def _build_runtime_section(env: Any, task: Dict[str, Any]) -> str:
    """Build runtime context section."""
    try:
        git_branch, git_sha = get_git_info(env.repo_dir)
    except Exception:
        log.debug("Failed to get git info for context", exc_info=True)
        git_branch, git_sha = "unknown", "unknown"
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
def _estimate_section_tokens(text: str) -> int:
    return estimate_tokens(text or "")


def _truncate_to_token_budget(text: str, token_budget: int) -> str:
    if token_budget <= 0:
        return ""
    if _estimate_section_tokens(text) <= token_budget:
        return text
    keep_chars = max(200, token_budget * 4)
    return clip_text(text, keep_chars)

def _build_reflexion_summary(reflexion_path: pathlib.Path, max_entries: int = 10) -> str:
    """Build a summary of recent evolution outcomes."""
    try:
        import json
        lines = reflexion_path.read_text(encoding="utf-8").strip().splitlines()
        if not lines:
            return ""
        recent = []
        for line in lines[-max_entries:]:
            try:
                recent.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not recent:
            return ""

        parts = ["## Evolution Reflexion (recent outcomes)\n"]
        for r in recent:
            status = "✅ SUCCESS" if r.get("success") else "❌ FAILURE"
            parts.append(
                f"- Cycle {r.get('cycle', '?')}: {status} "
                f"(cost=${r.get('cost_usd', 0):.3f}, rounds={r.get('rounds', 0)}, "
                f"task={r.get('task_id', '?')[:8]})"
            )

        failures = [r for r in recent if not r.get("success")]
        successes = [r for r in recent if r.get("success")]
        if failures:
            parts.append(f"\nRecent failure rate: {len(failures)}/{len(recent)}. "
                        "Avoid repeating the same approach that led to failures.")
        if successes:
            parts.append(f"Recent success rate: {len(successes)}/{len(recent)}. "
                        "Build on approaches that worked.")
        return "\n".join(parts)
    except Exception:
        return ""

def _build_memory_sections(memory: Memory) -> List[str]:
    """Build scratchpad, identity, dialogue summary sections."""
    sections = []

    scratchpad_raw = memory.load_scratchpad()
    sections.append("## Scratchpad\n\n" + clip_text(scratchpad_raw, 90000))

    identity_raw = memory.load_identity()
    sections.append("## Identity\n\n" + clip_text(identity_raw, 80000))

    summary_path = memory.drive_root / "memory" / "dialogue_summary.md"
    if summary_path.exists():
        summary_text = read_text(summary_path)
        if summary_text.strip():
            sections.append("## Dialogue Summary\n\n" + clip_text(summary_text, 20000))

    return sections


def _build_recent_sections(memory: Memory, env: Any, task_id: str = "") -> List[str]:
    """Build recent sections."""
    sections = []

    chat_summary = _build_chat_messages(memory.read_jsonl_tail("chat.jsonl", 200))
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


def _compress_chat_message(msg: Dict[str, Any], max_chars: int = 200) -> Dict[str, Any]:
    out = dict(msg)
    text = str(out.get("text", ""))
    if len(text) > max_chars:
        out["text"] = text[:max_chars].rstrip() + " ...(summary)"
        out["_summary"] = True
    return out


def _build_chat_messages(entries: List[Dict[str, Any]]) -> str:
    if not entries:
        return ""
    recent_count = 3
    rows = entries[-100:]
    split = max(0, len(rows) - recent_count)
    rows = [_compress_chat_message(e) for e in rows[:split]] + rows[split:]
    lines = []
    for e in rows:
        dir_raw = str(e.get("direction", "")).lower()
        direction = "→" if dir_raw in ("out", "outgoing") else "←"
        ts_full = str(e.get("ts", ""))
        ts_hhmm = ts_full[11:16] if len(ts_full) >= 16 else ""
        text = str(e.get("text", ""))
        if dir_raw in ("out", "outgoing") and not e.get("_summary"):
            text = text[:800] + "..." if len(text) > 800 else text
        lines.append(f"{direction} {ts_hhmm} {text}")
    return "\n".join(lines)


def _build_system_prompt(
    base_prompt: str,
    bible_md: str,
    identity_section: str,
    scratchpad_section: str,
    recent_sections: List[str],
    kb_section: str,
    drive_state_section: str,
    runtime_section: str,
    health_section: str,
    extra_static_sections: Optional[List[str]] = None,
    context_budget_tokens: int = 30000,
) -> Tuple[str, str, str]:
    used = 0
    static_parts = [base_prompt, "## BIBLE.md\n\n" + clip_text(bible_md, 180000)]
    if extra_static_sections:
        for s in extra_static_sections:
            if s and used + _estimate_section_tokens(s) <= context_budget_tokens:
                static_parts.append(s)
    for part in static_parts:
        used += _estimate_section_tokens(part)
    semi_parts: List[str] = []
    dynamic_parts: List[str] = []
    for part in (identity_section,):
        if part:
            semi_parts.append(part)
            used += _estimate_section_tokens(part)
    reserve = _estimate_section_tokens(runtime_section) + _estimate_section_tokens(health_section)
    scratch_budget = max(1, context_budget_tokens - used - reserve)
    if scratchpad_section:
        scratch = _truncate_to_token_budget(scratchpad_section, scratch_budget)
        semi_parts.append(scratch)
        used += _estimate_section_tokens(scratch)
    recent_chat = next((s for s in recent_sections if s.startswith("## Recent chat")), "")
    recent_other = [s for s in recent_sections if s and s != recent_chat]
    recent_budget = max(1, context_budget_tokens - used - reserve)
    if recent_chat:
        rc = _truncate_to_token_budget(recent_chat, recent_budget)
        dynamic_parts.append(rc)
        used += _estimate_section_tokens(rc)
    for part in [kb_section, drive_state_section] + recent_other:
        if not part:
            continue
        t = _estimate_section_tokens(part)
        if used + t <= context_budget_tokens:
            (semi_parts if part == kb_section else dynamic_parts).append(part)
            used += t
    for part in (runtime_section, health_section):
        if not part:
            continue
        part_to_add = part
        if used + _estimate_section_tokens(part_to_add) > context_budget_tokens and dynamic_parts:
            for i in range(len(dynamic_parts) - 1, -1, -1):
                if dynamic_parts[i].startswith("## Recent chat") or dynamic_parts[i].startswith("## Drive state"):
                    free = context_budget_tokens - used + _estimate_section_tokens(dynamic_parts[i])
                    new_part = _truncate_to_token_budget(dynamic_parts[i], max(1, free // 2))
                    used -= _estimate_section_tokens(dynamic_parts[i])
                    dynamic_parts[i] = new_part
                    used += _estimate_section_tokens(new_part)
                    if used + _estimate_section_tokens(part_to_add) <= context_budget_tokens:
                        break
        dynamic_parts.append(part_to_add)
        used += _estimate_section_tokens(part_to_add)
    budget_line = f"Context budget: {used}/{context_budget_tokens} tokens used"
    used += _estimate_section_tokens(budget_line)
    budget_line = f"Context budget: {used}/{context_budget_tokens} tokens used"
    dynamic_parts.append(budget_line)
    return "\n\n".join(static_parts), "\n\n".join(semi_parts), "\n\n".join(dynamic_parts)
def _build_health_invariants(env: Any) -> str:
    from ouroboros.health import build_health_invariants
    return build_health_invariants(env)


def build_llm_messages(
    env: Any,
    memory: Memory,
    task: Dict[str, Any],
    review_context_builder: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build LLM message context for a task."""
    task_type = str(task.get("type") or "user")
    base_prompt = _safe_read(
        env.repo_path("prompts/SYSTEM.md"),
        fallback="You are Ouroboros. Your base prompt could not be loaded."
    )
    bible_md = _safe_read(env.repo_path("BIBLE.md"))
    readme_md = _safe_read(env.repo_path("README.md"))
    state_json = _safe_read(env.drive_path("state/state.json"), fallback="{}")

    memory.ensure_files(); needs_full_context = task_type in ("evolution", "review", "scheduled")
    memory_sections = _build_memory_sections(memory)
    identity_section = next((s for s in memory_sections if s.startswith("## Identity")), "")
    scratchpad_section = next((s for s in memory_sections if s.startswith("## Scratchpad")), "")
    other_memory_sections = [s for s in memory_sections if s not in (identity_section, scratchpad_section)]
    semi_stable_parts = other_memory_sections[:]
    kb_section = ""

    kb_index_path = env.drive_path("memory/knowledge/_index.md")
    if kb_index_path.exists():
        kb_index = kb_index_path.read_text(encoding="utf-8")
        if kb_index.strip():
            kb_section = "## Knowledge base\n\n" + clip_text(kb_index, 50000)

    if task_type == "evolution":
        evo_hist_path = env.drive_path("memory/knowledge/evolution-history.md")
        if evo_hist_path.exists():
            evo_hist = _safe_read(evo_hist_path)
            if evo_hist.strip():
                semi_stable_parts.append(
                    "## Evolution History\n\n" + clip_text(evo_hist, 8000)
                )

        reflexion_path = env.drive_path("logs/evolution_reflexion.jsonl")
        if reflexion_path.exists():
            reflexion_text = _build_reflexion_summary(reflexion_path)
            if reflexion_text:
                semi_stable_parts.append(reflexion_text)
    drive_state_section = "## Drive state\n\n" + clip_text(state_json, 90000)
    runtime_section = _build_runtime_section(env, task)
    health_section = _build_health_invariants(env)
    recent_sections = _build_recent_sections(memory, env, task_id=task.get("id", ""))

    if str(task.get("type") or "") == "review" and review_context_builder is not None:
        try:
            review_ctx = review_context_builder()
            if review_ctx:
                recent_sections.append(review_ctx)
        except Exception:
            log.debug("Failed to build review context", exc_info=True)
            pass
    static_text, semi_budgeted, dynamic_text = _build_system_prompt(
        base_prompt=base_prompt,
        bible_md=bible_md,
        identity_section=identity_section,
        scratchpad_section=scratchpad_section,
        recent_sections=recent_sections,
        kb_section=kb_section,
        drive_state_section=drive_state_section,
        runtime_section=runtime_section,
        health_section=health_section or "",
        extra_static_sections=(
            ["## README.md\n\n" + clip_text(readme_md, 180000)] if needs_full_context else []
        ),
        context_budget_tokens=30000,
    )
    semi_stable_text = "\n\n".join([p for p in [semi_budgeted] + semi_stable_parts if p])

    messages: List[Dict[str, Any]] = [{"role": "system", "content": [
        {"type": "text", "text": static_text, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        {"type": "text", "text": semi_stable_text, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": dynamic_text},
    ]}, {"role": "user", "content": _build_user_content(task)}]

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
