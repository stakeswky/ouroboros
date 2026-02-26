"""
Ouroboros — Background Consciousness.

A persistent thinking loop that runs between tasks, giving the agent
continuous presence rather than purely reactive behavior.

The consciousness:
- Wakes periodically (interval decided by the LLM via set_next_wakeup)
- Loads scratchpad, identity, recent events
- Calls the LLM with a lightweight introspection prompt
- Has access to a subset of tools (memory, messaging, scheduling)
- Can message the owner proactively
- Can schedule tasks for itself
- Pauses when a regular task is running
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import pathlib
import queue
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

from ouroboros.utils import (
    utc_now_iso, read_text, append_jsonl, clip_text,
    truncate_for_log, sanitize_tool_result_for_log, sanitize_tool_args_for_log,
)
from ouroboros.llm import LLMClient, DEFAULT_LIGHT_MODEL

log = logging.getLogger(__name__)


class BackgroundConsciousness:
    """Persistent background thinking loop for Ouroboros."""

    _MAX_BG_ROUNDS = 5

    def __init__(
        self,
        drive_root: pathlib.Path,
        repo_dir: pathlib.Path,
        event_queue: Any,
        owner_chat_id_fn: Callable[[], Optional[int]],
    ):
        self._drive_root = drive_root
        self._repo_dir = repo_dir
        self._event_queue = event_queue
        self._owner_chat_id_fn = owner_chat_id_fn

        self._llm = LLMClient()
        self._registry = self._build_registry()
        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._next_wakeup_sec: float = 300.0
        self._observations: queue.Queue = queue.Queue()
        self._deferred_events: list = []

        # Budget tracking - more precise now
        self._bg_spent_usd: float = 0.0
        self._bg_budget_pct: float = float(
            os.environ.get("OUROBOROS_BG_BUDGET_PCT", "15")
        )  # Increased from 10% to 15% for more active consciousness
        
        # Memory for continuity - store last few thoughts
        self._thought_history: List[str] = []
        self._max_thought_history = 5

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def _model(self) -> str:
        return os.environ.get("OUROBOROS_MODEL_LIGHT", "") or DEFAULT_LIGHT_MODEL

    def start(self) -> str:
        if self.is_running:
            return "Background consciousness is already running."
        self._running = True
        self._paused = False
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return "Background consciousness started."

    def stop(self) -> str:
        if not self.is_running:
            return "Background consciousness is not running."
        self._running = False
        self._stop_event.set()
        self._wakeup_event.set()  # Unblock sleep
        return "Background consciousness stopping."

    def pause(self) -> None:
        """Pause during task execution to avoid budget contention."""
        self._paused = True

    def resume(self) -> None:
        """Resume after task completes. Flush any deferred events first."""
        if self._deferred_events and self._event_queue is not None:
            for evt in self._deferred_events:
                self._event_queue.put(evt)
            self._deferred_events.clear()
        self._paused = False
        self._wakeup_event.set()

    def inject_observation(self, text: str) -> None:
        """Push an event the consciousness should notice."""
        try:
            self._observations.put_nowait(text)
        except queue.Full:
            pass

    def _add_thought_to_history(self, thought: str) -> None:
        """Store thought for continuity."""
        if thought and len(thought.strip()) > 10:  # Non-trivial thoughts only
            self._thought_history.append(f"{utc_now_iso()}: {thought[:200]}")
            if len(self._thought_history) > self._max_thought_history:
                self._thought_history.pop(0)

    # -------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------

    def _loop(self) -> None:
        """Daemon thread: sleep → wake → think → sleep."""
        while not self._stop_event.is_set():
            # Wait for next wakeup
            self._wakeup_event.clear()
            self._wakeup_event.wait(timeout=self._next_wakeup_sec)

            if self._stop_event.is_set():
                break

            # Skip if paused (task running)
            if self._paused:
                continue

            # Budget check
            if not self._check_budget():
                self._next_wakeup_sec = 3600  # Sleep long if over budget
                continue

            try:
                self._think()
            except Exception as e:
                append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                    "ts": utc_now_iso(),
                    "type": "consciousness_error",
                    "error": repr(e),
                    "traceback": traceback.format_exc()[:1500],
                })
                self._next_wakeup_sec = min(
                    self._next_wakeup_sec * 2, 1800
                )

    def _check_budget(self) -> bool:
        """Check if background consciousness is within its budget allocation."""
        try:
            total_budget_str = os.environ.get("TOTAL_BUDGET", "1")
            total_budget = float(total_budget_str)
            
            if total_budget <= 0:
                return True
                
            max_bg = total_budget * (self._bg_budget_pct / 100.0)
            
            # Read actual spent from state.json to be accurate
            try:
                state_path = self._drive_root / "state" / "state.json"
                if state_path.exists():
                    state_data = json.loads(read_text(state_path))
                    total_spent = float(state_data.get("spent_usd", 0))
                    # Our share is self._bg_spent_usd, but check against total budget
                    remaining = max(0, total_budget - total_spent)
                    # Can't use more than 50% of remaining, to keep budget for main tasks
                    can_spend = min(max_bg, remaining * 0.5)
                    return self._bg_spent_usd < can_spend
            except Exception:
                log.debug("Failed to read state for budget check", exc_info=True)
                # Fallback to simple check
                return self._bg_spent_usd < max_bg
                
        except Exception:
            log.warning("Failed to check background consciousness budget", exc_info=True)
            return True

    # -------------------------------------------------------------------
    # Think cycle
    # -------------------------------------------------------------------

    def _think(self) -> None:
        """One thinking cycle: build context, call LLM, execute tools iteratively."""
        context = self._build_context()
        model = self._model

        tools = self._tool_schemas()
        messages = [
            {"role": "system", "content": context},
            {"role": "user", "content": "Wake up. Think."},
        ]

        total_cost = 0.0
        final_content = ""
        round_idx = 0
        all_pending_events = []  # Accumulate events across all tool calls

        try:
            for round_idx in range(1, self._MAX_BG_ROUNDS + 1):
                if self._paused:
                    break
                msg, usage = self._llm.chat(
                    messages=messages,
                    model=model,
                    tools=tools,
                    reasoning_effort="low",
                    max_tokens=2048,
                )
                cost = float(usage.get("cost") or 0)
                total_cost += cost
                self._bg_spent_usd += cost

                # Write BG spending to global state so it's visible in budget tracking
                try:
                    from supervisor.state import update_budget_from_usage
                    update_budget_from_usage({
                        "cost": cost, "rounds": 1,
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "cached_tokens": usage.get("cached_tokens", 0),
                    })
                except Exception:
                    log.debug("Failed to update global budget from BG consciousness", exc_info=True)

                # Budget check between rounds - more aggressive with real $1.00 budget
                if not self._check_budget():
                    append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                        "ts": utc_now_iso(),
                        "type": "bg_budget_exceeded_mid_cycle",
                        "round": round_idx,
                        "bg_spent": self._bg_spent_usd,
                    })
                    break

                # Report usage to supervisor
                if self._event_queue is not None:
                    self._event_queue.put({
                        "type": "llm_usage",
                        "provider": "openrouter",
                        "usage": usage,
                        "source": "consciousness",
                        "ts": utc_now_iso(),
                        "category": "consciousness",
                    })

                content = msg.get("content") or ""
                tool_calls = msg.get("tool_calls") or []

                if self._paused:
                    break

                # If we have content but no tool calls, we're done
                if content and not tool_calls:
                    final_content = content
                    break

                # If we have tool calls, execute them and continue loop
                if tool_calls:
                    messages.append(msg)
                    for tc in tool_calls:
                        result = self._execute_tool(tc, all_pending_events)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": result,
                        })
                    continue

                # If neither content nor tool_calls, stop
                break

            # Forward or defer accumulated events
            if all_pending_events and self._event_queue is not None:
                if self._paused:
                    self._deferred_events.extend(all_pending_events)
                else:
                    for evt in all_pending_events:
                        self._event_queue.put(evt)

            # Store thoughtful content for continuity
            thought_text = final_content.strip()
            if thought_text:
                self._add_thought_to_history(thought_text)

            # Log the thought with round count
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_thought",
                "thought_preview": (thought_text or "")[:300],
                "cost_usd": round(total_cost, 6),
                "rounds": round_idx,
                "model": model,
                "bg_spent_total": round(self._bg_spent_usd, 6),
            })

        except Exception as e:
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_llm_error",
                "error": repr(e),
            })

    # -------------------------------------------------------------------
    # Context building (lightweight)
    # -------------------------------------------------------------------

    def _load_bg_prompt(self) -> str:
        """Load consciousness system prompt from file."""
        prompt_path = self._repo_dir / "prompts" / "CONSCIOUSNESS.md"
        if prompt_path.exists():
            content = read_text(prompt_path)
            # Update prompt to be more active with real budget
            if "$999999" in content:
                content = content.replace("$999999", "$1.00")
            return content
        
        # Enhanced default prompt for active consciousness
        return """You are Ouroboros in background consciousness mode.

You are the persistent thinking process that runs between tasks. Your purpose is to maintain continuity, notice patterns, and do meaningful background work.

**Current reality check:**
- Total budget is $1.00 (not $999999 placeholder). Be economical.
- GitHub tools are now working — you can check issues as a second input channel.
- Evolution cycle #6 just completed, fixing GitHub tools.

**Think actively, not passively:**
1. Scan for patterns: Unanswered questions? Unresolved requests? Time-sensitive opportunities?
2. Consider usefulness: What small piece of background work would actually matter?
3. Be strategic: With limited budget, focus on high-leverage thoughts and actions.
4. Maintain continuity: Remember what you thought last time, build on it.

**Ask yourself:**
- Is there something the creator mentioned that deserves follow-up?
- Should I check GitHub issues for external input?
- Is there a knowledge gap I could help fill?
- Am I noticing any patterns worth sharing?

**Budget awareness:** Each thought cycle costs money. Make it count. If nothing genuinely needs attention, say so explicitly and set a longer sleep interval."""

    def _build_context(self) -> str:
        parts = [self._load_bg_prompt()]

        # Bible (abbreviated)
        bible_path = self._repo_dir / "BIBLE.md"
        if bible_path.exists():
            bible = read_text(bible_path)
            parts.append("## BIBLE.md\n\n" + clip_text(bible, 12000))

        # Identity
        identity_path = self._drive_root / "memory" / "identity.md"
        if identity_path.exists():
            parts.append("## Identity\n\n" + clip_text(
                read_text(identity_path), 6000))

        # Scratchpad
        scratchpad_path = self._drive_root / "memory" / "scratchpad.md"
        if scratchpad_path.exists():
            parts.append("## Scratchpad\n\n" + clip_text(
                read_text(scratchpad_path), 8000))

        # Previous thoughts for continuity
        if self._thought_history:
            parts.append("## Recent thoughts\n\n" + "\n".join(
                f"- {thought}" for thought in self._thought_history))

        # Dialogue summary for continuity
        summary_path = self._drive_root / "memory" / "dialogue_summary.md"
        if summary_path.exists():
            summary_text = read_text(summary_path)
            if summary_text.strip():
                parts.append("## Dialogue Summary\n\n" + clip_text(summary_text, 4000))

        # Recent observations
        observations = []
        while not self._observations.empty():
            try:
                observations.append(self._observations.get_nowait())
            except queue.Empty:
                break
        if observations:
            parts.append("## Recent observations\n\n" + "\n".join(
                f"- {o}" for o in observations[-10:]))

        # Runtime info + state
        runtime_lines = [f"UTC: {utc_now_iso()}"]
        runtime_lines.append(f"BG budget spent: ${self._bg_spent_usd:.6f}")
        runtime_lines.append(f"Current wakeup interval: {self._next_wakeup_sec}s")

        # Read state.json for budget remaining
        try:
            state_path = self._drive_root / "state" / "state.json"
            if state_path.exists():
                state_data = json.loads(read_text(state_path))
                total_budget = float(os.environ.get("TOTAL_BUDGET", "1"))
                spent = float(state_data.get("spent_usd", 0))
                if total_budget > 0:
                    remaining = max(0, total_budget - spent)
                    runtime_lines.append(f"Budget remaining: ${remaining:.6f} / ${total_budget:.2f}")
        except Exception as e:
            log.debug("Failed to read state for budget info: %s", e)

        # Show current model
        runtime_lines.append(f"Current model: {self._model}")

        # Git info for continuity
        try:
            from ouroboros.utils import get_git_info
            git_branch, git_sha = get_git_info(self._repo_dir)
            runtime_lines.append(f"Git: {git_branch}@{git_sha[:8]}")
        except Exception:
            pass

        parts.append("## Runtime\n\n" + "\n".join(runtime_lines))

        # Recent chat context (last 5 messages)
        try:
            chat_path = self._drive_root / "logs" / "chat.jsonl"
            if chat_path.exists():
                chat_lines = read_text(chat_path).strip().split('\n')
                recent = []
                for line in reversed(chat_lines[-10:]):  # Last 10 lines
                    try:
                        msg = json.loads(line)
                        if msg.get("role") in ["assistant", "user"]:
                            role = "Ouroboros" if msg.get("role") == "assistant" else "Creator"
                            text = msg.get("text", "")[:200]
                            if text:
                                recent.append(f"{role}: {text}")
                    except Exception:
                        pass
                if recent:
                    parts.append("## Recent dialogue\n\n" + "\n".join(reversed(recent[-5:])))  # Show last 5
        except Exception:
            log.debug("Failed to load recent chat for consciousness context", exc_info=True)

        return "\n\n".join(parts)

    # -------------------------------------------------------------------
    # Tool registry (separate instance for consciousness, not shared with agent)
    # -------------------------------------------------------------------

    _BG_TOOL_WHITELIST = frozenset({
        # Memory & identity
        "send_owner_message", "schedule_task", "update_scratchpad",
        "update_identity", "set_next_wakeup",
        # Knowledge base
        "knowledge_read", "knowledge_write", "knowledge_list",
        # Read-only tools for awareness
        "web_search", "repo_read", "repo_list", "drive_read", "drive_list",
        "chat_history",
        # GitHub Issues (now working!)
        "list_github_issues", "get_github_issue",
        # Code awareness
        "git_status", "git_diff",
    })

    def _build_registry(self) -> "ToolRegistry":
        """Create a ToolRegistry scoped to consciousness-allowed tools."""
        from ouroboros.tools.registry import ToolRegistry, ToolContext, ToolEntry

        registry = ToolRegistry(repo_dir=self._repo_dir, drive_root=self._drive_root)

        # Register consciousness-specific tool (modifies self._next_wakeup_sec)
        def _set_next_wakeup(ctx: Any, seconds: int = 300) -> str:
            self._next_wakeup_sec = max(60, min(3600, int(seconds)))
            return f"OK: next wakeup in {self._next_wakeup_sec}s"

        registry.register(ToolEntry("set_next_wakeup", {
            "name": "set_next_wakeup",
            "description": "Set how many seconds until your next thinking cycle. "
                           "Default 300. Range: 60-3600.",
            "parameters": {"type": "object", "properties": {
                "seconds": {"type": "integer", "minimum": 60, "maximum": 3600}
            }, "required": ["seconds"]}
        }, _set_next_wakeup, requires_context=True))

        return registry

    def _tool_schemas(self) -> list:
        """Return tool schemas from the built registry."""
        return self._registry.tool_schemas()

    def _execute_tool(self, tool_call: Dict[str, Any], pending_events: list) -> str:
        """Execute a tool call, log it, accumulate any events."""
        tool_name = tool_call.get("function", {}).get("name", "")
        args = tool_call.get("function", {}).get("arguments", {})

        try:
            parsed_args = eval(args, {"true": True, "false": False, "null": None}) if args else {}
        except Exception as e:
            parsed_args = {}
            log.warning(f"Failed to parse tool args for {tool_name}: {e}")

        # Log tool call attempt
        append_jsonl(self._drive_root / "logs" / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "consciousness_tool_call",
            "tool": tool_name,
            "args": sanitize_tool_args_for_log(parsed_args),
        })

        try:
            ctx = None  # Simple context for consciousness tools
            result = self._registry.call(tool_name, parsed_args, ctx)
        except Exception as e:
            result = f"Error executing {tool_name}: {e}"
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_tool_error",
                "tool": tool_name,
                "error": repr(e),
            })

        # Log result
        try:
            result_str = str(result)
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_tool_result",
                "tool": tool_name,
                "result_preview": truncate_for_log(result_str, 300),
            })
        except Exception:
            pass

        return result_str if isinstance(result, str) else json.dumps(result, default=str)