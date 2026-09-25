"""Durable state of the call-me-back plugin: Tasks, the event inbox, and the dedupe window.

Everything lives under one data directory (the plugin's Hermes data dir). The receiver CLI only ever
writes into ``inbox/``; the gateway process owns ``tasks.json`` and ``seen.json``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SEEN_LIMIT = 2000
REQUIRED_EVENT_FIELDS = ("id", "task", "executor", "reason")
REASONS = ("turn_end", "needs_approval", "session_exit")
EXECUTORS = ("claude-code", "codex", "pi")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:160]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def validate_event(event: Any) -> Optional[str]:
    """Return None when *event* is a usable stop event, otherwise the reason it is rejected."""
    if not isinstance(event, dict):
        return "payload is not a JSON object"
    missing = [k for k in REQUIRED_EVENT_FIELDS if not isinstance(event.get(k), str) or not event[k]]
    if missing:
        return f"missing fields: {', '.join(missing)}"
    if event["reason"] not in REASONS:
        return f"unknown reason {event['reason']!r}"
    if event["executor"] not in EXECUTORS:
        return f"unknown executor {event['executor']!r}"
    return None


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.inbox = self.root / "inbox"
        self.rejected = self.root / "rejected"
        self.tasks_path = self.root / "tasks.json"
        self.seen_path = self.root / "seen.json"
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ inbox
    def put_event(self, event: dict) -> Path:
        """Receiver side: drop one validated event into the inbox (idempotent per event id).

        A resend of an event still in the inbox is ignored: overwriting it would reset its progress and
        wake the conversation a second time.
        """
        self.inbox.mkdir(parents=True, exist_ok=True)
        target = self.inbox / f"{_safe_name(event['id'])}.json"
        if not target.exists():
            write_json(target, {**event, "_received": time.time()})
        return target

    def pending_events(self) -> list[Path]:
        try:
            files = [p for p in self.inbox.iterdir() if p.suffix == ".json" and not p.name.startswith(".")]
        except FileNotFoundError:
            return []
        # Arrival order (progress write-backs change mtime, so it cannot be used).
        return sorted(files, key=_received_at)

    def reject(self, path: Path, why: str) -> None:
        self.rejected.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(path, self.rejected / path.name)
            with open(self.rejected / f"{path.name}.why", "w", encoding="utf-8") as f:
                f.write(why)
        except OSError:
            pass

    # ------------------------------------------------------------------ dedupe
    def seen(self, event_id: str) -> bool:
        with self._lock:
            return event_id in _read_json(self.seen_path, [])

    def mark_seen(self, event_id: str) -> None:
        with self._lock:
            ids = _read_json(self.seen_path, [])
            if event_id not in ids:
                ids.append(event_id)
                write_json(self.seen_path, ids[-SEEN_LIMIT:])

    # ------------------------------------------------------------------ tasks
    def tasks(self) -> dict[str, dict]:
        with self._lock:
            return _read_json(self.tasks_path, {})

    def get_task(self, task_id: str) -> Optional[dict]:
        return self.tasks().get(task_id)

    def save_task(self, task: dict) -> dict:
        with self._lock:
            tasks = _read_json(self.tasks_path, {})
            tasks[task["id"]] = task
            write_json(self.tasks_path, tasks)
            return task

    def update_task(self, task_id: str, **changes: Any) -> Optional[dict]:
        with self._lock:
            tasks = _read_json(self.tasks_path, {})
            task = tasks.get(task_id)
            if task is None:
                return None
            task.update(changes)
            write_json(self.tasks_path, tasks)
            return task

    def record_event(self, task_id: str, event: dict) -> Optional[dict]:
        """Attach a processed stop event's summary to its Task (and remember the executor's session id)."""
        with self._lock:
            tasks = _read_json(self.tasks_path, {})
            task = tasks.get(task_id)
            if task is None:
                return None
            sessions = task.setdefault("agent_sessions", [])
            if event.get("agent_session") and event["agent_session"] not in sessions:
                sessions.append(event["agent_session"])
            task["events"] = int(task.get("events", 0)) + 1
            task["last_event"] = {
                "reason": event["reason"],
                "ts": event.get("ts") or now_iso(),
                "detail": event.get("detail", ""),
                "preview": (event.get("last_message") or "")[:200],
            }
            write_json(self.tasks_path, tasks)
            return task


def _received_at(path: Path) -> float:
    try:
        return float(_read_json(path, {}).get("_received") or path.stat().st_mtime)
    except (OSError, TypeError, ValueError, AttributeError):
        return 0.0


def new_task_id(title: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:24]
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    return f"T-{stamp}-{slug}" if slug else f"T-{stamp}"
