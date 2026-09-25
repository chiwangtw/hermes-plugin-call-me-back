"""call-me-back — let a Hermes agent reach CLI coding agent Sessions (Claude Code, Codex, pi) and be woken
when a Session it dispatched stops.

Pieces (vocabulary in CONTEXT.md, decisions in docs/adr/):
- tools: list / read / send to Sessions, dispatch a Task, list and close Tasks
- `hermes call-me-back receive`: the execution machine pipes stop events into this plugin's inbox
- a watcher thread inside the gateway: for each event of an open Task, notify the human through a
  deliver_only webhook route and wake the dispatching conversation with ctx.inject_message
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

from urllib.parse import quote

from . import events
from .store import Store, new_task_id, now_iso, validate_event, write_json, EXECUTORS
from .remote import Machine, MachineError

log = logging.getLogger(__name__)

DEFAULT_WEBHOOK_BASE = "http://127.0.0.1:8644/webhooks"
# Prompts an agent shows before its first turn; they block it before any stop hook can fire.
STARTUP_PROMPT = re.compile(r"trust (the contents of )?this (directory|folder)|Do you trust|Hooks need review|"
                            r"Press enter to continue", re.I)
GIVE_UP_AFTER = 3600  # seconds an event may keep failing before it is set aside
MAX_BACKOFF = 300


def _ok(**data) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False)


def _err(message: str, **data) -> str:
    return json.dumps({"ok": False, "error": message, **data}, ensure_ascii=False)


def _caller_session_key() -> str:
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return ""
    return get_session_env("HERMES_SESSION_KEY", "")


def _route_secret(route: str) -> str:
    """The HMAC secret Hermes checks for *route*: the route's own secret, else the global WEBHOOK_SECRET."""
    try:
        from hermes_constants import get_hermes_home
        subs = json.loads((get_hermes_home() / "webhook_subscriptions.json").read_text(encoding="utf-8"))
        secret = (subs.get(route) or {}).get("secret")
        if secret:
            return str(secret)
    except Exception:
        pass
    return os.environ.get("WEBHOOK_SECRET", "")


def _gateway_live() -> bool:
    """True when a gateway in this process can take injected turns right now.

    inject_message() returning True only means "scheduled": a gateway that is draining for a restart
    still accepts the request and then drops it, so hold events while it drains.
    """
    try:
        from hermes_cli.plugins import get_plugin_manager
        manager = get_plugin_manager()
        value = manager.has_gateway_message_injector
        if not (value() if callable(value) else value):
            return False
        owner = (getattr(manager, "_gateway_message_injector", None) or (None,))[0]
        return not (getattr(owner, "_draining", False) or getattr(owner, "_restart_requested", False))
    except Exception:
        return False


TOOL_SCHEMAS = {
    "call_me_back_list": {
        "description": "List live terminals on the execution machine (Orca). Shows which ones run a CLI agent "
                       "and which are bound to a call-me-back Task.",
        "parameters": {"type": "object", "properties": {
            "all": {"type": "boolean", "description": "Include terminals that are not running an agent."}}},
    },
    "call_me_back_read": {
        "description": "Read the current screen of a Session's terminal. Give either terminal (handle) or task.",
        "parameters": {"type": "object", "properties": {
            "terminal": {"type": "string"}, "task": {"type": "string"},
            "lines": {"type": "integer", "description": "Max lines (default 40)."}}},
    },
    "call_me_back_send": {
        "description": "Type text into a Session (and press Enter), or interrupt it. Give either terminal or task.",
        "parameters": {"type": "object", "properties": {
            "terminal": {"type": "string"}, "task": {"type": "string"},
            "text": {"type": "string"},
            "enter": {"type": "boolean", "description": "Press Enter after the text (default true)."},
            "interrupt": {"type": "boolean", "description": "Send an interrupt (Esc/Ctrl-C style) instead."}}},
    },
    "call_me_back_dispatch": {
        "description": "Dispatch a Task: start a CLI agent Session on the execution machine with this prompt. "
                       "Every time that Session stops (turn end, waiting for approval, exit) the human is "
                       "notified and THIS conversation is woken, until the Task is closed with call_me_back_close. "
                       "After dispatching, end your turn; do not poll. If the result says waiting_for_human, the "
                       "agent is stuck at a startup prompt shown in startup_screen: answer it with "
                       "call_me_back_send. Full guide: skill_view('call-me-back:guide').",
        "parameters": {"type": "object", "required": ["executor", "prompt"], "properties": {
            "executor": {"type": "string", "enum": list(EXECUTORS)},
            "prompt": {"type": "string", "description": "Instructions for the agent (any length)."},
            "worktree": {"type": "string", "description": "Existing directory on the execution machine to run in."},
            "repo": {"type": "string", "description": "Repo path on the execution machine; with new_worktree, "
                                                      "creates an Orca worktree for the Task."},
            "new_worktree": {"type": "string", "description": "Name of the worktree to create in repo."},
            "title": {"type": "string", "description": "Short title for the terminal tab and the Task id."}}},
    },
    "call_me_back_tasks": {
        "description": "List call-me-back Tasks with their state and last stop event.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["open", "closed", "all"], "description": "Default open."}}},
    },
    "call_me_back_close": {
        "description": "Close a Task: its Session keeps running, but stops no longer notify or wake anyone.",
        "parameters": {"type": "object", "required": ["task"], "properties": {
            "task": {"type": "string"}, "note": {"type": "string"}}},
    },
}


def register(ctx):
    store = Store(ctx.state.data_dir)

    def setting(key, default=None):
        value = ctx.get_config(key, default)
        return default if value is None else value

    def machine() -> Machine:
        m = Machine(host=setting("host", "local"), orca_bin=setting("orca_bin", "orca"),
                    bin_dir=setting("bin_dir", "~/.local/bin"),
                    state_dir=setting("state_dir", "~/.local/state/call-me-back"))
        m.executors.update(setting("executors", {}) or {})
        return m

    def terminal_of(params) -> tuple[str, str]:
        """Resolve (terminal handle, error) from a tool call's terminal/task arguments."""
        if params.get("terminal"):
            return params["terminal"], ""
        if params.get("task"):
            task = store.get_task(params["task"])
            if not task:
                return "", f"unknown task {params['task']}"
            if not task.get("terminal"):
                return "", f"task {params['task']} has no terminal handle"
            return task["terminal"], ""
        return "", "give terminal or task"

    # ------------------------------------------------------------------ tools
    def t_list(params, **_):
        try:
            result = machine().orca("terminal", "list")
        except MachineError as exc:
            return _err(str(exc))
        by_terminal = {t.get("terminal"): tid for tid, t in store.tasks().items() if t.get("status") == "open"}
        now_ms = time.time() * 1000
        rows = []
        for t in result.get("terminals", []):
            if not params.get("all") and not t.get("agentIdentity") and t.get("handle") not in by_terminal:
                continue
            last = t.get("lastOutputAt")
            rows.append({"terminal": t.get("handle"), "title": t.get("title"), "agent": t.get("agentIdentity"),
                         "worktree": t.get("worktreePath"), "task": by_terminal.get(t.get("handle")),
                         "idle_s": int((now_ms - last) / 1000) if last else None,
                         "preview": (t.get("preview") or "")[:80]})
        return _ok(terminals=rows, total=result.get("totalCount"))

    def t_read(params, **_):
        handle, why = terminal_of(params)
        if not handle:
            return _err(why)
        try:
            term = machine().orca("terminal", "read", "--terminal", handle, "--screen",
                                 "--limit", str(int(params.get("lines") or 40))).get("terminal", {})
        except MachineError as exc:
            return _err(str(exc))
        return _ok(terminal=handle, status=term.get("status"), draft=term.get("draft"),
                   screen="\n".join(term.get("tail") or []))

    def t_send(params, **_):
        handle, why = terminal_of(params)
        if not handle:
            return _err(why)
        w = machine()
        try:
            if params.get("interrupt"):
                return _ok(result=w.orca("terminal", "send", "--terminal", handle, "--interrupt"))
            args = ["terminal", "send", "--terminal", handle, "--text", params.get("text") or ""]
            if params.get("enter", True):
                args += ["--enter", "--wait-submit", "5"]
            return _ok(result=w.orca(*args))
        except MachineError as exc:
            return _err(str(exc))

    def t_dispatch(params, **_):
        executor, prompt = params.get("executor"), params.get("prompt") or ""
        if executor not in EXECUTORS:
            return _err(f"executor must be one of {', '.join(EXECUTORS)}")
        if not prompt.strip():
            return _err("prompt is empty")
        title = (params.get("title") or "").strip()
        task_id = new_task_id(title or params.get("new_worktree") or "")
        while store.get_task(task_id):
            task_id = new_task_id(title or params.get("new_worktree") or "")
        w = machine()
        try:
            path = params.get("worktree")
            if params.get("new_worktree"):
                if not params.get("repo"):
                    return _err("new_worktree needs repo")
                created = w.orca("worktree", "create", "--repo", f"path:{params['repo']}",
                                 "--name", params["new_worktree"], "--no-parent", timeout=180)
                path = (created.get("worktree") or {}).get("path") or created.get("path")
            if not path:
                return _err("give worktree, or repo + new_worktree")
            prompt_path = w.write_prompt(task_id, prompt)
            create = ("terminal", "create", "--worktree", f"path:{path}", "--title", title or task_id,
                      "--command", w.run_command(task_id, executor, prompt_path))
            try:
                created = w.orca(*create)
            except MachineError as exc:
                if exc.code != "selector_not_found":
                    raise
                # Orca opens terminals only in folders it knows; register this one and try once more.
                w.orca("repo", "add", "--path", path)
                created = w.orca(*create)
        except MachineError as exc:
            return _err(str(exc), task=task_id)
        handle = created.get("handle") or (created.get("terminal") or {}).get("handle")
        key = _caller_session_key()
        store.save_task({"id": task_id, "status": "open", "executor": executor, "title": title,
                         "machine": w.host, "worktree": path, "terminal": handle, "conversation": key,
                         "created": now_iso(), "prompt_head": prompt[:200], "events": 0, "agent_sessions": []})
        note = "" if key else " (no gateway conversation: this Task will notify but cannot wake anyone)"
        result = {"task": task_id, "terminal": handle, "worktree": path,
                  "message": f"Dispatched. Stops will notify the human and wake this conversation.{note}"}
        # Startup prompts (folder trust, hook review) block the agent before any hook can fire, so no stop
        # event would ever arrive. Look at the screen once and hand the prompt back to the caller.
        wait = float(setting("startup_check_seconds", 8) or 0)
        if wait > 0 and handle:
            time.sleep(wait)
            try:
                screen = "\n".join(machine().orca("terminal", "read", "--terminal", handle, "--screen",
                                                  "--limit", "15").get("terminal", {}).get("tail") or [])
            except MachineError as exc:
                screen = f"(could not read screen: {exc})"
            result["startup_screen"] = screen.strip()[-1200:]
            if STARTUP_PROMPT.search(screen):
                result["waiting_for_human"] = True
                result["message"] += (" The Session is stuck at a startup prompt (see startup_screen) and will not "
                                      "report anything until it is answered: answer it with call_me_back_send, "
                                      "or ask the human.")
        return _ok(**result)

    def t_tasks(params, **_):
        status = params.get("status") or "open"
        rows = [t for t in store.tasks().values() if status == "all" or t.get("status") == status]
        rows.sort(key=lambda t: t.get("created", ""), reverse=True)
        return _ok(tasks=[{k: t.get(k) for k in ("id", "status", "executor", "title", "terminal", "worktree",
                                                  "created", "events", "last_event", "closed")} for t in rows])

    def t_close(params, **_):
        task = store.get_task(params.get("task") or "")
        if not task:
            return _err(f"unknown task {params.get('task')}")
        store.update_task(task["id"], status="closed", closed=now_iso(), close_note=params.get("note") or "")
        try:
            w = machine()
            w.host = task.get("machine") or w.host
            w.forget(task["id"])
            forgot = True
        except MachineError as exc:
            log.warning("call-me-back: forget %s on the execution machine failed: %s", task["id"], exc)
            forgot = False
        return _ok(task=task["id"], status="closed", machine_bindings_cleared=forgot)

    handlers = {"call_me_back_list": t_list, "call_me_back_read": t_read, "call_me_back_send": t_send,
                "call_me_back_dispatch": t_dispatch, "call_me_back_tasks": t_tasks, "call_me_back_close": t_close}
    for name, handler in handlers.items():
        ctx.register_tool(name=name, toolset="call_me_back", schema={"name": name, **TOOL_SCHEMAS[name]},
                          handler=handler)

    # ------------------------------------------------------------------ CLI
    def cli_setup(parser):
        sub = parser.add_subparsers(dest="call_me_back_cmd")
        sub.add_parser("receive", help="Read one stop event (JSON) from stdin into the inbox")
        tasks = sub.add_parser("tasks", help="Print Tasks as JSON")
        tasks.add_argument("--all", action="store_true")
        sub.add_parser("where", help="Print the plugin's data directory")

    def cli_handler(args):
        cmd = getattr(args, "call_me_back_cmd", None)
        if cmd == "receive":
            raw = sys.stdin.buffer.read().decode("utf-8", "replace")
            try:
                event = json.loads(raw)
            except ValueError:
                print("rejected: not JSON", file=sys.stderr)
                return 2
            why = validate_event(event)
            if why:
                print(f"rejected: {why}", file=sys.stderr)
                return 2
            store.put_event(event)
            print(f"stored {event['id']}")
            return 0
        if cmd == "tasks":
            rows = [t for t in store.tasks().values() if args.all or t.get("status") == "open"]
            print(json.dumps(rows, ensure_ascii=False, indent=1))
            return 0
        if cmd == "where":
            print(store.root)
            return 0
        print("usage: hermes call-me-back {receive,tasks,where}", file=sys.stderr)
        return 64

    ctx.register_cli_command(name="call-me-back", help="call-me-back: event receiver and Task inspection",
                             setup_fn=cli_setup, handler_fn=cli_handler)

    guide = Path(__file__).parent / "skills" / "guide" / "SKILL.md"
    if guide.exists():
        ctx.register_skill("guide", guide, description="How to dispatch, take over and close call-me-back Tasks")

    # ------------------------------------------------------------------ watcher
    def handle_event(path):
        try:
            with open(path, encoding="utf-8") as f:
                event = json.load(f)
        except (OSError, ValueError) as exc:
            store.reject(path, f"unreadable: {exc}")
            return
        why = validate_event(event)
        if why:
            store.reject(path, why)
            return
        eid = event["id"]
        if store.seen(eid):
            path.unlink(missing_ok=True)
            return
        task = store.get_task(event["task"])
        if task is None:
            store.reject(path, f"unknown task {event['task']}")
            store.mark_seen(eid)
            return
        if task.get("status") != "open":
            log.info("call-me-back: drop %s (task %s is %s)", eid, task["id"], task.get("status"))
            store.mark_seen(eid)
            path.unlink(missing_ok=True)
            return

        # Every finished step is written back before the next one, so a retry (or a crash, or a gateway
        # restart) never records, wakes or notifies twice.
        progress = event.setdefault("_progress", {})
        progress.setdefault("first_try", time.time())
        if time.time() < progress.get("next_try", 0):
            return

        language = str(setting("language", "en"))

        def step_done(name):
            progress[name] = True
            write_json(path, event)

        try:
            if not progress.get("recorded"):
                task = store.record_event(task["id"], event) or task
                step_done("recorded")
            if not progress.get("woken"):
                key = task.get("conversation")
                if key and not ctx.inject_message(events.wake_text(event, task, language), session_key=key):
                    raise RuntimeError("gateway did not accept the wake-up")
                step_done("woken")
            if not progress.get("notified"):
                route = setting("notify_route", "")
                if route:
                    url = f"{setting('webhook_base', DEFAULT_WEBHOOK_BASE).rstrip('/')}/{quote(str(route), safe='')}"
                    ok, why = events.post_webhook(url, _route_secret(str(route)),
                                                  {"text": events.notification_text(event, task, language),
                                                   "task": event["task"]}, request_id=eid)
                    if not ok:
                        raise RuntimeError(f"notify failed: {why}")
                step_done("notified")
        except Exception as exc:
            attempts = int(progress.get("attempts", 0)) + 1
            if time.time() - progress["first_try"] > GIVE_UP_AFTER:
                log.warning("call-me-back: giving up on %s after %d attempts: %s", eid, attempts, exc)
                store.mark_seen(eid)
                store.reject(path, f"gave up after {attempts} attempts: {exc}")
                return
            progress.update(attempts=attempts, last_error=str(exc)[:300],
                            next_try=time.time() + min(MAX_BACKOFF, 2 ** attempts))
            write_json(path, event)
            log.warning("call-me-back: %s attempt %d failed: %s", eid, attempts, exc)
            return
        store.mark_seen(eid)
        path.unlink(missing_ok=True)
        log.info("call-me-back: %s %s -> task %s handled", event["executor"], event["reason"], task["id"])

    stop = threading.Event()

    def watch():
        while not stop.is_set():
            poll = 2.0
            try:
                poll = float(setting("poll_seconds", 2) or 2)
                if _gateway_live():
                    for path in store.pending_events():
                        try:
                            handle_event(path)
                        except FileNotFoundError:
                            pass  # handled by a concurrent watcher (e.g. during a plugin reload)
                        except Exception:
                            log.exception("call-me-back: failed to handle %s", path.name)
            except Exception:
                log.exception("call-me-back: watcher loop error")
            stop.wait(poll)

    thread = threading.Thread(target=watch, name="call-me-back-watcher", daemon=True)
    thread.start()
    ctx.on_unload(stop.set)
