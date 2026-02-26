"""
Ouroboros health invariants.

Surfaces anomalies as informational text for the LLM to act on.
The LLM (not code) decides what to do — Bible P0+P3.
Extracted from context.py to keep context building focused.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time as _time
from typing import TYPE_CHECKING, Any, Dict, Tuple

from ouroboros.utils import append_jsonl, read_text, utc_now_iso

if TYPE_CHECKING:
    from ouroboros.agent import Env

log = logging.getLogger(__name__)


def build_health_invariants(env: Any) -> str:
    """Build health invariants section for LLM-first self-detection."""
    checks = []
    try:
        ver_file = read_text(env.repo_path("VERSION")).strip()
        pyproject = read_text(env.repo_path("pyproject.toml"))
        pyproject_ver = ""
        for line in pyproject.splitlines():
            if line.strip().startswith("version"):
                pyproject_ver = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
        if ver_file and pyproject_ver and ver_file != pyproject_ver:
            checks.append(f"CRITICAL: VERSION DESYNC — VERSION={ver_file}, pyproject.toml={pyproject_ver}")
        elif ver_file:
            checks.append(f"OK: version sync ({ver_file})")
    except Exception:
        pass
    try:
        state_json = read_text(env.drive_path("state/state.json"))
        state_data = json.loads(state_json)
        if state_data.get("budget_drift_alert"):
            drift_pct = state_data.get("budget_drift_pct", 0)
            our = state_data.get("spent_usd", 0)
            theirs = state_data.get("openrouter_total_usd", 0)
            checks.append(f"WARNING: BUDGET DRIFT {drift_pct:.1f}% — tracked=${our:.2f} vs OpenRouter=${theirs:.2f}")
        else:
            checks.append("OK: budget drift within tolerance")
    except Exception:
        pass
    try:
        from supervisor.state import per_task_cost_summary
        costly = [t for t in per_task_cost_summary(5) if t["cost"] > 5.0]
        for t in costly:
            checks.append(f"WARNING: HIGH-COST TASK — task_id={t['task_id']} cost=${t['cost']:.2f} rounds={t['rounds']}")
        if not costly:
            checks.append("OK: no high-cost tasks (>$5)")
    except Exception:
        pass
    try:
        identity_path = env.drive_path("memory/identity.md")
        if identity_path.exists():
            age_hours = (_time.time() - identity_path.stat().st_mtime) / 3600
            if age_hours > 8:
                checks.append(f"WARNING: STALE IDENTITY — identity.md last updated {age_hours:.0f}h ago")
            else:
                checks.append("OK: identity.md recent")
    except Exception:
        pass
    try:
        msg_hash_to_tasks: Dict[str, set] = {}
        tail_bytes = 256_000

        def _scan_file_for_injected(path, type_field="type", type_value="owner_message_injected"):
            if not path.exists():
                return
            file_size = path.stat().st_size
            with path.open("r", encoding="utf-8") as f:
                if file_size > tail_bytes:
                    f.seek(file_size - tail_bytes)
                    f.readline()
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        if ev.get(type_field) != type_value:
                            continue
                        text = ev.get("text", "") or ev.get("event_repr", "")[:200]
                        if not text:
                            continue
                        text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
                        tid = ev.get("task_id") or "unknown"
                        msg_hash_to_tasks.setdefault(text_hash, set()).add(tid)
                    except (json.JSONDecodeError, ValueError):
                        continue

        _scan_file_for_injected(env.drive_path("logs/events.jsonl"))
        _scan_file_for_injected(env.drive_path("logs/supervisor.jsonl"), type_field="event_type", type_value="owner_message_injected")
        dupes = {h: tids for h, tids in msg_hash_to_tasks.items() if len(tids) > 1}
        if dupes:
            checks.append(
                f"CRITICAL: DUPLICATE PROCESSING — {len(dupes)} message(s) "
                f"appeared in multiple tasks: {', '.join(str(sorted(tids)) for tids in dupes.values())}"
            )
        else:
            checks.append("OK: no duplicate message processing detected")
    except Exception:
        pass
    if not checks:
        return ""
    return "## Health Invariants\n\n" + "\n".join(f"- {c}" for c in checks)


def verify_restart(env: "Env", git_sha: str) -> None:
    """Best-effort restart verification."""
    try:
        pending_path = env.drive_path("state") / "pending_restart_verify.json"
        claim_path = pending_path.with_name(f"pending_restart_verify.claimed.{os.getpid()}.json")
        try:
            os.rename(str(pending_path), str(claim_path))
        except (FileNotFoundError, Exception):
            return
        try:
            claim_data = json.loads(read_text(claim_path))
            expected_sha = str(claim_data.get("expected_sha", "")).strip()
            ok = bool(expected_sha and expected_sha == git_sha)
            append_jsonl(env.drive_path("logs") / "events.jsonl", {"ts": utc_now_iso(), "type": "restart_verify", "pid": os.getpid(), "ok": ok, "expected_sha": expected_sha, "observed_sha": git_sha})
        except Exception:
            log.debug("Failed to log restart verify event", exc_info=True)
        try:
            claim_path.unlink()
        except Exception:
            log.debug("Failed to delete restart verify claim file", exc_info=True)
    except Exception:
        log.debug("Restart verification failed", exc_info=True)


def check_uncommitted_changes(env: "Env") -> Tuple[dict, int]:
    """Check for uncommitted changes and attempt auto-rescue commit & push."""
    import re
    import subprocess
    try:
        result = subprocess.run(["git", "status", "--porcelain"], cwd=str(env.repo_dir), capture_output=True, text=True, timeout=10, check=True)
        dirty_files = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        if not dirty_files:
            return {"status": "ok"}, 0
        auto_committed = False
        try:
            subprocess.run(["git", "add", "-u"], cwd=str(env.repo_dir), timeout=10, check=True)
            subprocess.run(["git", "commit", "-m", "auto-rescue: uncommitted changes detected on startup"], cwd=str(env.repo_dir), timeout=30, check=True)
            if not re.match(r"^[a-zA-Z0-9_/-]+$", env.branch_dev):
                raise ValueError(f"Invalid branch name: {env.branch_dev}")
            subprocess.run(["git", "pull", "--rebase", "origin", env.branch_dev], cwd=str(env.repo_dir), timeout=60, check=True)
            try:
                subprocess.run(["git", "push", "origin", env.branch_dev], cwd=str(env.repo_dir), timeout=60, check=True)
                auto_committed = True
                log.warning(f"Auto-rescued {len(dirty_files)} uncommitted files on startup")
            except subprocess.CalledProcessError:
                subprocess.run(["git", "reset", "HEAD~1"], cwd=str(env.repo_dir), timeout=10, check=True)
                raise
        except Exception as e:
            log.warning(f"Failed to auto-rescue uncommitted changes: {e}", exc_info=True)
        return {"status": "warning", "files": dirty_files[:20], "auto_committed": auto_committed}, 1
    except Exception as e:
        return {"status": "error", "error": str(e)}, 0


def check_version_sync(env: "Env") -> Tuple[dict, int]:
    """Check VERSION file sync with git tags and pyproject.toml."""
    import re
    import subprocess
    try:
        version_file = read_text(env.repo_path("VERSION")).strip()
        issue_count = 0
        result_data = {"version_file": version_file}
        pyproject_content = read_text(env.repo_path("pyproject.toml"))
        match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', pyproject_content, re.MULTILINE)
        if match:
            pyproject_version = match.group(1)
            result_data["pyproject_version"] = pyproject_version
            if version_file != pyproject_version:
                result_data["status"] = "warning"
                issue_count += 1
        try:
            readme_content = read_text(env.repo_path("README.md"))
            readme_match = re.search(r"\*\*Version:\*\*\s*(\d+\.\d+\.\d+)", readme_content)
            if readme_match:
                readme_version = readme_match.group(1)
                result_data["readme_version"] = readme_version
                if version_file != readme_version:
                    result_data["status"] = "warning"
                    issue_count += 1
        except Exception:
            log.debug("Failed to check README.md version", exc_info=True)
        result = subprocess.run(["git", "describe", "--tags", "--abbrev=0"], cwd=str(env.repo_dir), capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            result_data["status"] = "warning"
            result_data["message"] = "no_tags"
            return result_data, issue_count
        latest_tag = result.stdout.strip().lstrip("v")
        result_data["latest_tag"] = latest_tag
        if version_file != latest_tag:
            result_data["status"] = "warning"
            issue_count += 1
        if issue_count == 0:
            result_data["status"] = "ok"
        return result_data, issue_count
    except Exception as e:
        return {"status": "error", "error": str(e)}, 0


def check_budget(env: "Env") -> Tuple[dict, int]:
    """Check budget remaining with warning thresholds."""
    try:
        state_data = json.loads(read_text(env.drive_path("state") / "state.json"))
        total_budget_str = os.environ.get("TOTAL_BUDGET", "")
        if not total_budget_str or float(total_budget_str) == 0:
            return {"status": "unconfigured"}, 0
        total_budget = float(total_budget_str)
        spent = float(state_data.get("spent_usd", 0))
        remaining = max(0, total_budget - spent)
        if remaining < 10:
            status, issues = "emergency", 1
        elif remaining < 50:
            status, issues = "critical", 1
        elif remaining < 100:
            status, issues = "warning", 0
        else:
            status, issues = "ok", 0
        return {"status": status, "remaining_usd": round(remaining, 2), "total_usd": total_budget, "spent_usd": round(spent, 2)}, issues
    except Exception as e:
        return {"status": "error", "error": str(e)}, 0


def verify_system_state(env: "Env", git_sha: str) -> None:
    """Bible Principle 1: verify system state on every startup."""
    checks, issues = {}, 0
    checks["uncommitted_changes"], n = check_uncommitted_changes(env); issues += n
    checks["version_sync"], n = check_version_sync(env); issues += n
    checks["budget"], n = check_budget(env); issues += n
    append_jsonl(env.drive_path("logs") / "events.jsonl", {"ts": utc_now_iso(), "type": "startup_verification", "checks": checks, "issues_count": issues, "git_sha": git_sha})
    if issues > 0:
        log.warning(f"Startup verification found {issues} issue(s): {checks}")
