import json
import platform
import shutil
import subprocess
import sys
import time

from src.executor import task_registry
from src.executor.task_validator import TaskValidator

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 900
MAX_OUTPUT_CHARS = 64_000
MAX_ARGS = 16


def _truncate(text):
    if not text:
        return ""
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + "\n...[truncated]"
    return text


def _result(entry, ok, exit_code=None, stdout="", stderr="", error=None, started=None):
    stdout = _truncate(stdout)
    return {
        "ok": ok,
        "task_id": entry["id"] if entry else None,
        "task_type": entry["type"] if entry else None,
        "exit_code": exit_code,
        "stdout": stdout,
        # What a caller almost always wants: the payload without the trailing
        # newline every script and console writer appends.
        "output": stdout.strip(),
        "stderr": _truncate(stderr),
        "error": error,
        "duration_ms": None if started is None else int((time.monotonic() - started) * 1000),
    }


class TaskRunner:

    def __init__(self, validator=None):
        self.validator = validator or TaskValidator()

    # ----------------------------------------------------------------------
    # Entry point
    # ----------------------------------------------------------------------

    def executor(self, task):
        started = time.monotonic()

        entry = self.validator.resolve(task)
        if entry is None:
            return _result(None, False, error="validation_failed", started=started)

        args = self._extract_args(task, entry)
        if args is None:
            return _result(entry, False, error="invalid_args", started=started)

        handler = self._HANDLERS.get(entry["type"])
        if handler is None:
            return _result(entry, False, error="unsupported_task_type", started=started)

        timeout = self._extract_timeout(task)

        try:
            return handler(self, entry, args, timeout, started)
        except Exception as exc:  # a bad task must not take the runner down
            return _result(entry, False, error=f"{type(exc).__name__}: {exc}", started=started)

    # ----------------------------------------------------------------------
    # Payload extraction
    # ----------------------------------------------------------------------

    @staticmethod
    def _extract_args(task, entry):
        """Return a list of string args, or None if the payload is invalid."""
        raw = task.get("args")
        if raw in (None, []):
            return []

        if not entry["allow_args"]:
            # Args are opt-in per registry entry so a caller cannot start
            # feeding flags to a script that was not written to expect them.
            return None
        if not isinstance(raw, list) or not all(isinstance(a, str) for a in raw):
            return None
        if len(raw) > MAX_ARGS:
            return None
        if any("\x00" in a for a in raw):
            return None

        if not TaskRunner._args_permitted(raw, entry):
            return None

        return list(raw)

    @staticmethod
    def _args_permitted(args, entry):
        """Every arg must be allowlisted verbatim, or match the task's pattern.

        The model chooses these, so without this check a hallucinated or
        injected flag would reach the script.
        """
        allowlist = entry["args_allowlist"]
        pattern = entry.get("args_pattern")

        if not allowlist and not pattern:
            return True

        for arg in args:
            if allowlist and arg in allowlist:
                continue
            # Values that cannot be enumerated (an app name) are admitted by
            # pattern instead. The pattern is what bounds them: no quotes,
            # semicolons, or pipes, and a hard length limit.
            if pattern and pattern.fullmatch(arg):
                continue
            return False

        return True

    @staticmethod
    def _extract_timeout(task):
        raw = task.get("timeout")
        if isinstance(raw, int) and not isinstance(raw, bool) and 0 < raw <= MAX_TIMEOUT:
            return raw
        return DEFAULT_TIMEOUT

    # ----------------------------------------------------------------------
    # Subprocess plumbing
    # ----------------------------------------------------------------------

    def _run_process(self, entry, argv, timeout, started):
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,  # argv list only; nothing is handed to a shell
                cwd=str(task_registry.SCRIPTS_DIR),
                stdin=subprocess.DEVNULL,  # a script that reads stdin must not hang
            )
        except subprocess.TimeoutExpired:
            return _result(entry, False, error="timeout", started=started)
        except OSError as exc:
            return _result(entry, False, error=f"spawn_failed: {exc}", started=started)

        ok = completed.returncode == 0
        return _result(
            entry,
            ok,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            # A non-zero exit needs a stated reason. Without one the caller gets
            # ok=false with error=null and has to dig through stderr to guess.
            error=None if ok else self._exit_error(entry, completed.returncode),
            started=started,
        )

    @staticmethod
    def _exit_error(entry, exit_code):
        """A named reason when the registry declares one, else the raw exit."""
        named = entry.get("errors_by_exit", {}).get(exit_code)
        return named or f"task_failed: exit {exit_code}"

    # ----------------------------------------------------------------------
    # Handlers, one per task type
    # ----------------------------------------------------------------------

    def _run_powershell(self, entry, args, timeout, started):
        script = task_registry.resolve_script(entry["executor"])
        if script is None:
            return _result(entry, False, error="script_not_found", started=started)

        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            return _result(entry, False, error="powershell_not_available", started=started)

        argv = [
            shell,
            "-NoProfile",
            "-NonInteractive",
            # Our own scripts, resolved inside SCRIPTS_DIR. Without this the
            # machine's execution policy decides whether the agent works.
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *args,
        ]
        return self._run_process(entry, argv, timeout, started)

    def _run_python(self, entry, args, timeout, started):
        script = task_registry.resolve_script(entry["executor"])
        if script is None:
            return _result(entry, False, error="script_not_found", started=started)

        argv = [sys.executable, str(script), *args]
        return self._run_process(entry, argv, timeout, started)

    def _run_builtin(self, entry, args, timeout, started):
        """In-process handlers. No subprocess, no script file on disk."""
        func = self._BUILTINS.get(entry["executor"])
        if func is None:
            return _result(entry, False, error="unknown_builtin", started=started)
        return _result(entry, True, exit_code=0, stdout=func(), started=started)

    def _run_noop(self, entry, args, timeout, started):
        """Health check: proves the pipeline works without side effects."""
        return _result(entry, True, exit_code=0, stdout="ok", started=started)

    _HANDLERS = {
        "powershell": _run_powershell,
        "python": _run_python,
        "builtin": _run_builtin,
        "noop": _run_noop,
    }

    _BUILTINS = {
        "system-info": lambda: json.dumps(
            {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            }
        ),
        "ping": lambda: "pong",
    }
