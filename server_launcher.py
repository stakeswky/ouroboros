# ============================
# Ouroboros — Server launcher (no Colab dependency)
# ============================

import logging
import os, sys, json, time, uuid, pathlib, subprocess, datetime, threading, queue as _queue_mod
from typing import Any, Dict, List, Optional, Set, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ----------------------------
# 0) Install deps
# ----------------------------
def install_launcher_deps() -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "openai>=1.0.0", "requests"],
        check=True,
    )

install_launcher_deps()

from ouroboros.apply_patch import install as install_apply_patch
from ouroboros.llm import DEFAULT_LIGHT_MODEL
install_apply_patch()

# ----------------------------
# 1) Config — all hardcoded for server deployment
# ----------------------------
OPENROUTER_API_KEY = "sk-oNk1Adz6q3FrVDVU3kT68ooQ97q0FT5mBfXXwHmIFyj5ls7f"
TELEGRAM_BOT_TOKEN = "7633641953:AAFI9FDpehDVS1rikp8i6D08eElpXo58xjc"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER = "stakeswky"
GITHUB_REPO = "ouroboros"
TOTAL_BUDGET_LIMIT = 999999.0  # unlimited

OPENAI_API_KEY = OPENROUTER_API_KEY  # reuse for web search via oogg.top
ANTHROPIC_API_KEY = ""

MODEL_MAIN = "claude-sonnet-4-6"
MODEL_CODE = "claude-sonnet-4-6"
MODEL_LIGHT = "claude-sonnet-4-6"

MAX_WORKERS = 3  # conservative for 1.9G RAM
BUDGET_REPORT_EVERY_MESSAGES = 10
SOFT_TIMEOUT_SEC = 600
HARD_TIMEOUT_SEC = 1800
DIAG_HEARTBEAT_SEC = 30
DIAG_SLOW_CYCLE_SEC = 20

os.environ["OPENROUTER_API_KEY"] = OPENROUTER_API_KEY
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY
os.environ["GITHUB_USER"] = GITHUB_USER
os.environ["GITHUB_REPO"] = GITHUB_REPO
os.environ["OUROBOROS_MODEL"] = MODEL_MAIN
os.environ["OUROBOROS_MODEL_CODE"] = MODEL_CODE
os.environ["OUROBOROS_MODEL_LIGHT"] = MODEL_LIGHT
os.environ["OUROBOROS_DIAG_HEARTBEAT_SEC"] = str(DIAG_HEARTBEAT_SEC)
os.environ["OUROBOROS_DIAG_SLOW_CYCLE_SEC"] = str(DIAG_SLOW_CYCLE_SEC)
os.environ["TELEGRAM_BOT_TOKEN"] = TELEGRAM_BOT_TOKEN

# ----------------------------
# 2) Data directory (replaces Google Drive)
# ----------------------------
DRIVE_ROOT = pathlib.Path("/root/ouroboros_data").resolve()
REPO_DIR = pathlib.Path("/root/ouroboros").resolve()

for sub in ["state", "logs", "memory", "index", "locks", "archive"]:
    (DRIVE_ROOT / sub).mkdir(parents=True, exist_ok=True)

# Clear stale owner mailbox
try:
    from ouroboros.owner_inject import get_pending_path
    _stale_inject = get_pending_path(DRIVE_ROOT)
    if _stale_inject.exists():
        _stale_inject.unlink(missing_ok=True)
    _mailbox_dir = DRIVE_ROOT / "memory" / "owner_mailbox"
    if _mailbox_dir.exists():
        for _f in _mailbox_dir.iterdir():
            _f.unlink(missing_ok=True)
except Exception:
    pass

CHAT_LOG_PATH = DRIVE_ROOT / "logs" / "chat.jsonl"
if not CHAT_LOG_PATH.exists():
    CHAT_LOG_PATH.write_text("", encoding="utf-8")

# ----------------------------
# 3) Git constants
# ----------------------------
BRANCH_DEV = "ouroboros"
BRANCH_STABLE = "ouroboros-stable"
REMOTE_URL = f"https://{GITHUB_TOKEN}:x-oauth-basic@github.com/{GITHUB_USER}/{GITHUB_REPO}.git" if GITHUB_TOKEN else ""

# ----------------------------
# 4) Initialize supervisor modules
# ----------------------------
from supervisor.state import (
    init as state_init, load_state, save_state, append_jsonl,
    update_budget_from_usage, status_text, rotate_chat_log_if_needed,
    init_state,
)
state_init(DRIVE_ROOT, TOTAL_BUDGET_LIMIT)
init_state()

from supervisor.telegram import (
    init as telegram_init, TelegramClient, send_with_budget, log_chat,
)
TG = TelegramClient(str(TELEGRAM_BOT_TOKEN))
telegram_init(
    drive_root=DRIVE_ROOT,
    total_budget_limit=TOTAL_BUDGET_LIMIT,
    budget_report_every=BUDGET_REPORT_EVERY_MESSAGES,
    tg_client=TG,
)

from supervisor.git_ops import (
    init as git_ops_init, ensure_repo_present, checkout_and_reset,
    sync_runtime_dependencies, import_test, safe_restart,
)
git_ops_init(
    repo_dir=REPO_DIR, drive_root=DRIVE_ROOT, remote_url=REMOTE_URL,
    branch_dev=BRANCH_DEV, branch_stable=BRANCH_STABLE,
)

from supervisor.queue import (
    enqueue_task, enforce_task_timeouts, enqueue_evolution_task_if_needed,
    persist_queue_snapshot, restore_pending_from_snapshot,
    cancel_task_by_id, queue_review_task, sort_pending,
)

from supervisor.workers import (
    init as workers_init, get_event_q, WORKERS, PENDING, RUNNING,
    spawn_workers, kill_workers, assign_tasks, ensure_workers_healthy,
    handle_chat_direct, _get_chat_agent, auto_resume_after_restart,
)
workers_init(
    repo_dir=REPO_DIR, drive_root=DRIVE_ROOT, max_workers=MAX_WORKERS,
    soft_timeout=SOFT_TIMEOUT_SEC, hard_timeout=HARD_TIMEOUT_SEC,
    total_budget_limit=TOTAL_BUDGET_LIMIT,
    branch_dev=BRANCH_DEV, branch_stable=BRANCH_STABLE,
)

from supervisor.events import dispatch_event

# ----------------------------
# 5) Bootstrap repo (skip git ops if no GITHUB_TOKEN)
# ----------------------------
if GITHUB_TOKEN and GITHUB_USER:
    ensure_repo_present()
    ok, msg = safe_restart(reason="bootstrap", unsynced_policy="rescue_and_reset")
    if not ok:
        log.warning(f"Bootstrap warning: {msg}")
else:
    log.info("No GITHUB_TOKEN set — skipping git bootstrap. Running from local repo.")

# ----------------------------
# 6) Start workers
# ----------------------------
kill_workers()
spawn_workers(MAX_WORKERS)
restored_pending = restore_pending_from_snapshot()
persist_queue_snapshot(reason="startup")
if restored_pending > 0:
    st_boot = load_state()
    if st_boot.get("owner_chat_id"):
        send_with_budget(int(st_boot["owner_chat_id"]),
                         f"♻️ Restored pending queue from snapshot: {restored_pending} tasks.")

append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "type": "launcher_start",
    "branch": load_state().get("current_branch"),
    "sha": load_state().get("current_sha"),
    "max_workers": MAX_WORKERS,
    "model_default": MODEL_MAIN, "model_code": MODEL_CODE, "model_light": MODEL_LIGHT,
    "soft_timeout_sec": SOFT_TIMEOUT_SEC, "hard_timeout_sec": HARD_TIMEOUT_SEC,
    "diag_heartbeat_sec": DIAG_HEARTBEAT_SEC,
    "diag_slow_cycle_sec": DIAG_SLOW_CYCLE_SEC,
})

auto_resume_after_restart()

# ----------------------------
# 6.2) Direct-mode watchdog
# ----------------------------
def reset_chat_agent():
    import supervisor.workers as _w
    _w._chat_agent = None

def _chat_watchdog_loop():
    soft_warned = False
    while True:
        time.sleep(30)
        try:
            agent = _get_chat_agent()
            if not agent._busy:
                soft_warned = False
                continue
            now = time.time()
            idle_sec = now - agent._last_progress_ts
            total_sec = now - agent._task_started_ts
            if idle_sec >= HARD_TIMEOUT_SEC:
                st = load_state()
                if st.get("owner_chat_id"):
                    send_with_budget(int(st["owner_chat_id"]),
                        f"⚠️ Task stuck ({int(total_sec)}s). Restarting agent.")
                reset_chat_agent()
                soft_warned = False
                continue
            if idle_sec >= SOFT_TIMEOUT_SEC and not soft_warned:
                soft_warned = True
                st = load_state()
                if st.get("owner_chat_id"):
                    send_with_budget(int(st["owner_chat_id"]),
                        f"⏱️ Task running {int(total_sec)}s, last progress {int(idle_sec)}s ago.")
        except Exception:
            pass

_watchdog_thread = threading.Thread(target=_chat_watchdog_loop, daemon=True)
_watchdog_thread.start()

# ----------------------------
# 6.3) Background consciousness
# ----------------------------
from ouroboros.consciousness import BackgroundConsciousness

def _get_owner_chat_id() -> Optional[int]:
    try:
        st = load_state()
        cid = st.get("owner_chat_id")
        return int(cid) if cid else None
    except Exception:
        return None

_consciousness = BackgroundConsciousness(
    drive_root=DRIVE_ROOT,
    repo_dir=REPO_DIR,
    event_queue=get_event_q(),
    owner_chat_id_fn=_get_owner_chat_id,
)

# ----------------------------
# 7) Main loop (copied from colab_launcher.py with Colab deps removed)
# ----------------------------
import types
_event_ctx = types.SimpleNamespace(
    DRIVE_ROOT=DRIVE_ROOT,
    REPO_DIR=REPO_DIR,
    BRANCH_DEV=BRANCH_DEV,
    BRANCH_STABLE=BRANCH_STABLE,
    TG=TG,
    WORKERS=WORKERS,
    PENDING=PENDING,
    RUNNING=RUNNING,
    MAX_WORKERS=MAX_WORKERS,
    send_with_budget=send_with_budget,
    load_state=load_state,
    save_state=save_state,
    update_budget_from_usage=update_budget_from_usage,
    append_jsonl=append_jsonl,
    enqueue_task=enqueue_task,
    cancel_task_by_id=cancel_task_by_id,
    queue_review_task=queue_review_task,
    persist_queue_snapshot=persist_queue_snapshot,
    safe_restart=safe_restart,
    kill_workers=kill_workers,
    spawn_workers=spawn_workers,
    sort_pending=sort_pending,
    consciousness=_consciousness,
)

def _safe_qsize(q: Any) -> int:
    try:
        return int(q.qsize())
    except Exception:
        return -1

def _handle_supervisor_command(text: str, chat_id: int, tg_offset: int = 0):
    lowered = text.strip().lower()
    if lowered.startswith("/panic"):
        send_with_budget(chat_id, "🛑 PANIC: stopping everything now.")
        kill_workers()
        st2 = load_state()
        st2["tg_offset"] = tg_offset
        save_state(st2)
        raise SystemExit("PANIC")
    if lowered.startswith("/restart"):
        st2 = load_state()
        st2["session_id"] = uuid.uuid4().hex
        st2["tg_offset"] = tg_offset
        save_state(st2)
        send_with_budget(chat_id, "♻️ Restarting (soft).")
        if GITHUB_TOKEN:
            ok, msg = safe_restart(reason="owner_restart", unsynced_policy="rescue_and_reset")
            if not ok:
                send_with_budget(chat_id, f"⚠️ Restart cancelled: {msg}")
                return True
        kill_workers()
        os.execv(sys.executable, [sys.executable, __file__])
    if lowered.startswith("/status"):
        status = status_text(WORKERS, PENDING, RUNNING, SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC)
        send_with_budget(chat_id, status, force_budget=True)
        return "[Supervisor handled /status]\n"
    if lowered.startswith("/review"):
        queue_review_task(reason="owner:/review", force=True)
        return "[Supervisor handled /review]\n"
    if lowered.startswith("/evolve"):
        parts = lowered.split()
        action = parts[1] if len(parts) > 1 else "on"
        turn_on = action not in ("off", "stop", "0")
        st2 = load_state()
        st2["evolution_mode_enabled"] = bool(turn_on)
        save_state(st2)
        if not turn_on:
            PENDING[:] = [t for t in PENDING if str(t.get("type")) != "evolution"]
            sort_pending()
            persist_queue_snapshot(reason="evolve_off")
        state_str = "ON" if turn_on else "OFF"
        send_with_budget(chat_id, f"🧬 Evolution: {state_str}")
        return f"[Supervisor handled /evolve — {state_str}]\n"
    if lowered.startswith("/bg"):
        parts = lowered.split()
        action = parts[1] if len(parts) > 1 else "status"
        if action in ("start", "on", "1"):
            result = _consciousness.start()
            send_with_budget(chat_id, f"🧠 {result}")
        elif action in ("stop", "off", "0"):
            result = _consciousness.stop()
            send_with_budget(chat_id, f"🧠 {result}")
        else:
            bg_status = "running" if _consciousness.is_running else "stopped"
            send_with_budget(chat_id, f"🧠 Background consciousness: {bg_status}")
        return f"[Supervisor handled /bg {action}]\n"
    return ""

offset = int(load_state().get("tg_offset") or 0)
_last_diag_heartbeat_ts = 0.0
_last_message_ts: float = time.time()
_ACTIVE_MODE_SEC: int = 300

# Auto-start background consciousness
try:
    _consciousness.start()
    log.info("🧠 Background consciousness auto-started")
except Exception as e:
    log.warning("consciousness auto-start failed: %s", e)

log.info("🐍 Ouroboros server launcher ready. Entering main loop...")

while True:
    loop_started_ts = time.time()
    rotate_chat_log_if_needed(DRIVE_ROOT)
    ensure_workers_healthy()

    # Drain worker events
    event_q = get_event_q()
    while True:
        try:
            ev = event_q.get_nowait()
        except Exception:
            break
        dispatch_event(ev, _event_ctx)

    # Assign queued tasks to free workers
    assign_tasks()
    enforce_task_timeouts()
    enqueue_evolution_task_if_needed()

    # Poll Telegram
    _now = time.time()
    try:
        updates = TG.get_updates(offset=offset, timeout=1)
    except Exception as e:
        log.warning("TG poll error: %s", e)
        time.sleep(2)
        continue

    for upd in updates:
        offset = upd["update_id"] + 1
        msg = upd.get("message")
        if not msg:
            continue
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")
        if not text:
            continue

        _last_message_ts = _now

        # Owner registration
        st = load_state()
        if not st.get("owner_chat_id"):
            st["owner_chat_id"] = chat_id
            save_state(st)
            send_with_budget(chat_id, "👁️ Creator registered. I am Ouroboros.")

        if chat_id != st.get("owner_chat_id"):
            continue

        log_chat(chat_id, "user", text)

        # Supervisor commands
        cmd_result = _handle_supervisor_command(text, chat_id, offset)
        if cmd_result is True:
            continue

        # Direct chat
        prefix = cmd_result if isinstance(cmd_result, str) else ""
        try:
            handle_chat_direct(
                text=prefix + text,
                chat_id=chat_id,
                tg=TG,
                drive_root=DRIVE_ROOT,
                repo_dir=REPO_DIR,
            )
        except Exception as e:
            log.error("chat_direct error: %s", e, exc_info=True)
            send_with_budget(chat_id, f"⚠️ Error: {e}")

    # Save offset
    st = load_state()
    st["tg_offset"] = offset
    save_state(st)

    now_epoch = time.time()
    loop_duration_sec = now_epoch - loop_started_ts

    if DIAG_SLOW_CYCLE_SEC > 0 and loop_duration_sec >= float(DIAG_SLOW_CYCLE_SEC):
        append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "main_loop_slow_cycle",
            "duration_sec": round(loop_duration_sec, 3),
        })

    if DIAG_HEARTBEAT_SEC > 0 and (now_epoch - _last_diag_heartbeat_ts) >= float(DIAG_HEARTBEAT_SEC):
        append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "main_loop_heartbeat",
            "offset": offset,
            "workers_total": len(WORKERS),
            "workers_alive": sum(1 for w in WORKERS.values() if w.proc.is_alive()),
            "pending_count": len(PENDING),
            "running_count": len(RUNNING),
            "event_q_size": _safe_qsize(event_q),
        })
        _last_diag_heartbeat_ts = now_epoch

    _loop_sleep = 0.1 if (_now - _last_message_ts) < _ACTIVE_MODE_SEC else 0.5
    time.sleep(_loop_sleep)
