"""Codex CLI — delegate complex coding tasks to gpt-5.3-codex via OpenAI Codex CLI."""

from __future__ import annotations

import json
import subprocess
from typing import List

from ouroboros.tools.registry import ToolContext, ToolEntry


def _codex_exec(ctx: ToolContext, prompt: str, approval_mode: str = "full-auto") -> str:
    """Run a coding task via Codex CLI (non-interactive exec mode)."""
    # exec subcommand supports: --full-auto (sandboxed) or --dangerously-bypass-approvals-and-sandbox
    # "full-auto" → sandboxed workspace-write; anything else → bypass (needed for actual file writes in Colab)
    if approval_mode == "full-auto":
        mode_flag = "--full-auto"
    else:
        mode_flag = "--dangerously-bypass-approvals-and-sandbox"

    try:
        result = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", mode_flag, "--json", prompt],
            capture_output=True, text=True, timeout=300,
            cwd=str(ctx.repo_dir),
        )
        # Try to extract last agent message from JSONL output
        last_message = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("type") == "message" and event.get("role") == "assistant":
                    last_message = event.get("content", "")
            except json.JSONDecodeError:
                pass

        output = last_message or result.stdout
        output = output[-8000:] if len(output) > 8000 else output
        stderr = result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr
        return json.dumps({
            "exit_code": result.returncode,
            "output": output,
            "stderr": stderr,
        }, ensure_ascii=False)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Codex CLI timed out after 300s"})
    except Exception as e:
        return json.dumps({"error": repr(e)})


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("codex_exec", {
            "name": "codex_exec",
            "description": "Delegate a complex coding task to OpenAI Codex CLI (gpt-5.3-codex). Use for: multi-file refactors, debugging, code review from a second model perspective. The task runs in full-auto mode in the repo directory.",
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string", "description": "Detailed coding task description"},
                "approval_mode": {
                    "type": "string",
                    "enum": ["full-auto", "auto-edit"],
                    "description": "full-auto: sandboxed workspace writes. auto-edit: bypass sandbox (needed for actual file edits in Colab). Default: full-auto",
                },
            }, "required": ["prompt"]},
        }, _codex_exec, timeout_sec=360),
    ]
