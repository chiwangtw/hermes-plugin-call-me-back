// call-me-back adapter for pi: agent_settled / ui_prompt_start / session_shutdown -> call-me-back-emit.
// Contract: machine/CONTRACT.md. Verified facts about pi 0.85.1: NOTES.md next to this file.
//
// Every event becomes at most one detached `call-me-back-emit --executor pi ...` process; pi never waits
// on it and nothing is printed. Untracked sessions (no CALL_ME_BACK_TASK, no binding) spawn nothing.
//
// No package import on purpose: the pi package was renamed (@mariozechner -> @earendil-works), so the
// few shapes used here are typed locally.

import { type ChildProcess, spawn } from "node:child_process";
import { closeSync, existsSync, openSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

type SessionEntry = { type?: string; message?: { role?: string; content?: unknown; stopReason?: string; errorMessage?: string } };
type Ctx = {
  cwd?: string;
  isIdle?: () => boolean;
  sessionManager?: { getSessionId?: () => string | undefined; getBranch?: () => SessionEntry[] };
};
type PiApi = { on: (event: string, handler: (event: any, ctx: Ctx) => unknown) => void };

const EXECUTOR = "pi";
let seq = 0;
let previous: ChildProcess | undefined;

// If the previous emit is still running, the next one waits for it (max ~5s) inside a detached sh, so events
// reach the spool in order (print mode fires turn_end and session_exit in the same millisecond) while pi
// itself never waits.
const WAIT_THEN_EXEC =
  'i=0; while [ $i -lt 100 ] && kill -0 "$1" 2>/dev/null; do sleep 0.05; i=$((i+1)); done; shift; exec "$@"';

// ~/.local/bin is often not on PATH (macOS default PATH, GUI-launched shells); spawn would fail silently.
function defaultEmit(): string {
  const local = join(process.env.HOME || "", ".local/bin/call-me-back-emit");
  return existsSync(local) ? local : "call-me-back-emit";
}

function sessionId(ctx: Ctx): string {
  try {
    return ctx.sessionManager?.getSessionId?.() ?? "";
  } catch {
    return "";
  }
}

// Same rule as call-me-back-emit: tracked if CALL_ME_BACK_TASK is set or a binding for this session exists.
function tracked(session: string): boolean {
  if (process.env.CALL_ME_BACK_TASK) return true;
  if (!session) return false;
  const state = process.env.CALL_ME_BACK_STATE_DIR || join(process.env.HOME || "", ".local/state/call-me-back");
  const safe = session.replace(/[^A-Za-z0-9_.-]/g, "_").slice(0, 80);
  try {
    return existsSync(join(state, "bindings", EXECUTOR, safe));
  } catch {
    return false;
  }
}

// Fire and forget. The message goes through an already-unlinked temp file as the child's stdin, so nothing
// is left to flush when pi exits right after (print mode, quit).
function emit(ctx: Ctx, reason: string, opts: { detail?: string; message?: string } = {}): void {
  const session = sessionId(ctx);
  if (!tracked(session)) return;
  const args = ["--executor", EXECUTOR, "--reason", reason, "--cwd", ctx.cwd || process.cwd()];
  if (session) args.push("--agent-session", session);
  if (opts.detail) args.push("--detail", opts.detail);
  let stdin: number | "ignore" = "ignore";
  try {
    if (opts.message !== undefined) {
      const file = join(tmpdir(), `call-me-back-pi-${process.pid}-${Date.now()}-${seq++}.txt`);
      writeFileSync(file, opts.message, { mode: 0o600 });
      stdin = openSync(file, "r");
      unlinkSync(file);
      args.push("--message-stdin");
    }
    const cmd = process.env.CALL_ME_BACK_EMIT || defaultEmit();
    const prev = previous && previous.exitCode === null && previous.signalCode === null ? previous.pid : undefined;
    const child = prev
      ? spawn("/bin/sh", ["-c", WAIT_THEN_EXEC, "sh", String(prev), cmd, ...args], { detached: true, stdio: [stdin, "ignore", "ignore"] })
      : spawn(cmd, args, { detached: true, stdio: [stdin, "ignore", "ignore"] });
    child.on("error", () => {});
    child.unref();
    previous = child;
  } catch {
    // Reporting must never disturb the agent.
  } finally {
    if (typeof stdin === "number") {
      try {
        closeSync(stdin);
      } catch {}
    }
  }
}

function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((p) => p && typeof p === "object" && p.type === "text" && typeof p.text === "string")
    .map((p) => p.text)
    .join("");
}

function lastAssistant(ctx: Ctx): SessionEntry["message"] | undefined {
  try {
    const branch = ctx.sessionManager?.getBranch?.() ?? [];
    for (let i = branch.length - 1; i >= 0; i--) {
      const m = branch[i]?.message;
      if (branch[i]?.type === "message" && m?.role === "assistant") return m;
    }
  } catch {}
  return undefined;
}

export default function (pi: PiApi) {
  let running = false;

  pi.on("agent_start", () => {
    running = true;
  });

  // agent_settled, not agent_end: pi may still retry, compact-and-retry or run queued follow-ups after agent_end.
  pi.on("agent_settled", (_event, ctx) => {
    running = false;
    const last = lastAssistant(ctx);
    // A turn the human aborted (Esc, or /new mid-turn) is not a stop to report: the human is at the keyboard.
    if (last?.stopReason === "aborted") return;
    let text = textOf(last?.content);
    if (!text && last?.stopReason === "error" && last.errorMessage) text = `[error] ${last.errorMessage}`;
    emit(ctx, "turn_end", { message: text, detail: last?.stopReason === "error" ? "error" : "" });
  });

  // pi has no built-in approval prompt. Permission-gate extensions block a tool with ctx.ui.confirm/select,
  // which pi brackets with ui_prompt_start. Only a prompt raised while the agent is running blocks it; a
  // prompt from a command the human just typed needs no notification.
  pi.on("ui_prompt_start", (event, ctx) => {
    let busy = running;
    try {
      if (!busy && typeof ctx?.isIdle === "function") busy = ctx.isIdle() === false;
    } catch {}
    if (!busy) return;
    const detail = [event?.kind, event?.title].filter(Boolean).join(": ").replace(/\s+/g, " ").slice(0, 200);
    emit(ctx, "needs_approval", { detail: detail || "ui_prompt" });
  });

  // reason: quit | reload | new | resume | fork. Only quit ends the process; the rest are in-process resets.
  pi.on("session_shutdown", (event, ctx) => {
    running = false;
    if (event?.reason !== "quit") return;
    // The dispatch wrapper reports exits of the Sessions it started (crashes included).
    if (process.env.CALL_ME_BACK_WRAPPED) return;
    emit(ctx, "session_exit", { detail: "quit", message: "Session ended (quit)" });
  });
}
