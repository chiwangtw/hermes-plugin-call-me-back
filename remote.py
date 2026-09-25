"""The execution machine: run a shell command there (locally or over SSH) and drive Orca."""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


MAX_INLINE = 20000  # base64 chars per ssh command line


class MachineError(RuntimeError):
    def __init__(self, message: str, code: str = "", output: str = ""):
        super().__init__(message)
        self.code, self.output = code, output


@dataclass
class Machine:
    host: str = "local"  # SSH alias of the execution machine, or "local" when it is this machine
    orca_bin: str = "orca"
    bin_dir: str = "~/.local/bin"  # where call-me-back-run / call-me-back-emit are installed
    state_dir: str = "~/.local/state/call-me-back"
    executors: dict = field(default_factory=lambda: {"claude-code": ["claude"], "codex": ["codex"], "pi": ["pi"]})
    timeout: int = 60

    @property
    def is_local(self) -> bool:
        return self.host in ("", "local", "localhost")

    # ---------------------------------------------------------------- shell
    def sh(self, command: str, *, stdin: Optional[str] = None, timeout: Optional[int] = None) -> str:
        """Run a POSIX shell command on the execution machine; return stdout or raise MachineError.

        *stdin* becomes a here-document for the command. Over SSH the whole script travels base64-encoded
        on the command line and ssh's own stdin is NUL: Windows re-quotes command lines (mangling quotes
        and non-ASCII text), and Windows OpenSSH hangs when its stdin or stdout is a pipe.
        """
        script = command
        if stdin is not None:
            delim = f"CALL_ME_BACK_EOF_{uuid.uuid4().hex}"
            script = f"{command} <<'{delim}'\n{stdin}\n{delim}"
        if self.is_local:
            return self._run(["/bin/sh", "-c", script], command, timeout)
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        if len(encoded) <= MAX_INLINE:
            return self._run(self._ssh(f"printf %s {encoded} | base64 -d | /bin/sh"), command, timeout)
        # Too long for one command line (Windows caps it near 32K): upload in pieces, then run.
        tmp = f"/tmp/call-me-back-{uuid.uuid4().hex}.b64"
        for i in range(0, len(encoded), MAX_INLINE):
            op = ">" if i == 0 else ">>"
            self._run(self._ssh(f"printf %s {encoded[i:i + MAX_INLINE]} {op} {tmp}"), command, timeout)
        return self._run(self._ssh(f"base64 -d < {tmp} | /bin/sh; rc=$?; rm -f {tmp}; exit $rc"), command, timeout)

    def _ssh(self, remote: str) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", self.host, remote]

    def _run(self, argv: list[str], label: str, timeout: Optional[int]) -> str:
        # stdout/stderr go to temp files, not pipes (see sh()).
        with tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
            try:
                proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=fout, stderr=ferr,
                                      timeout=timeout or self.timeout)
            except subprocess.TimeoutExpired as exc:
                raise MachineError(f"timed out after {exc.timeout}s: {label[:120]}") from exc
            except OSError as exc:
                raise MachineError(f"cannot start {argv[0]}: {exc}") from exc
            fout.seek(0)
            ferr.seek(0)
            out, err = fout.read().decode("utf-8", "replace"), ferr.read().decode("utf-8", "replace")
        if proc.returncode != 0:
            where = "local" if self.is_local else f"ssh {self.host}"
            raise MachineError(f"[{where}] exit {proc.returncode}: {(err or out).strip()[-600:]}", output=out)
        return out

    @staticmethod
    def quote(args: list[str]) -> str:
        """Quote every argument literally. Tool input (text, titles, paths from the caller) goes here."""
        return " ".join(shlex.quote(a) for a in args)

    @staticmethod
    def path(value: str) -> str:
        """Quote a configured path, expanding a leading ~/ on the execution machine. Never for tool input."""
        if value.startswith("~/"):
            return '"$HOME"/' + shlex.quote(value[2:])
        return shlex.quote(value)

    # ---------------------------------------------------------------- orca
    def orca(self, *args: str, parse_json: bool = True, timeout: Optional[int] = None) -> Any:
        try:
            out = self.sh(f"{self.path(self.orca_bin)} {self.quote([*args, *(['--json'] if parse_json else [])])}",
                          timeout=timeout)
        except MachineError as exc:
            # Orca exits non-zero with a JSON error on stdout; surface its code and hints, not a truncated tail.
            raise self._orca_error(exc.output) or exc
        if not parse_json:
            return out
        try:
            data = json.loads(out)
        except ValueError as exc:
            raise MachineError(f"orca returned non-JSON: {out[:300]}") from exc
        if isinstance(data, dict) and data.get("ok") is False:
            raise self._orca_error(out) or MachineError(f"orca error: {out[:400]}")
        return data.get("result", data) if isinstance(data, dict) else data

    @staticmethod
    def _orca_error(output: str) -> Optional[MachineError]:
        try:
            error = json.loads(output).get("error") or {}
        except (ValueError, AttributeError):
            return None
        hints = " ".join((error.get("data") or {}).get("nextSteps") or [])
        code = error.get("code") or "error"
        message = error.get("message") if error.get("message") != code else ""
        return MachineError(f"orca {code}: {' '.join(filter(None, [message, hints]))}"[:700], code=code, output=output)

    # ---------------------------------------------------------------- call-me-back scripts
    def write_prompt(self, task_id: str, prompt: str) -> str:
        """Store the prompt as a file on the execution machine (long argv through ssh/orca is lossy)."""
        path = f"{self.state_dir}/prompts/{task_id}.txt"
        self.sh(f"mkdir -p {self.path(self.state_dir + '/prompts')} && cat > {self.path(path)}", stdin=prompt)
        return path

    def run_command(self, task_id: str, executor: str, prompt_path: str) -> str:
        argv = self.executors.get(executor)
        if not argv:
            raise MachineError(f"no command configured for executor {executor!r}")
        return " ".join([self.path(f"{self.bin_dir}/call-me-back-run"),
                         self.quote(["--task", task_id, "--executor", executor, "--prompt-file"]),
                         self.path(prompt_path), "--", self.quote(argv)])

    def forget(self, task_id: str) -> None:
        self.sh(f"{self.path(self.bin_dir + '/call-me-back-emit')} {self.quote(['--forget', task_id])}")
