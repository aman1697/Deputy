"""Drives the calendar sign-in flow, so nobody has to run PowerShell by hand.

The device-code flow takes as long as the person takes to approve it, which is
far too long to hold an HTTP request open. So `start_login` starts the script,
waits only for the line carrying the code, and returns that while the script
keeps polling in the background. `is_signed_in` is how a caller finds out
whether the approval ever landed.
"""

import json
import queue
import shutil
import subprocess
import threading
import time

from src.executor import task_registry
from src.utils.constants import GRAPH_TOKEN_FILE
from src.utils.helpers import get_logger

logger = get_logger(__name__)

CALENDAR_SCRIPT = "get-calendar.ps1"

# A sign-in already in flight, so asking twice reuses the open browser tab and
# the code the user is already looking at instead of issuing a second one.
_pending = None
_pending_lock = threading.Lock()

# Stop offering a code shortly before it actually dies, rather than handing out
# one that expires while the user is still typing it.
PENDING_EXPIRY_SKEW_SECONDS = 30

# The code is issued by the first HTTP round trip, so it arrives quickly. This
# only has to be generous enough to cover a cold PowerShell start.
CODE_WAIT_SECONDS = 45

# -Logout only deletes a file; it has no reason to take longer than this.
LOGOUT_TIMEOUT_SECONDS = 30


class CalendarAuthError(RuntimeError):
    """Sign-in could not be started, or the script never produced a code."""


def is_signed_in() -> bool:
    """True when a cached token exists.

    Presence, not validity: the script owns refreshing, and a token that has
    gone stale surfaces as `not_signed_in_to_calendar` on the next read.
    """
    return GRAPH_TOKEN_FILE.is_file()


def _powershell() -> str:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        raise CalendarAuthError("powershell_not_available")
    return shell


def _script_path():
    script = task_registry.resolve_script(CALENDAR_SCRIPT)
    if script is None:
        raise CalendarAuthError(f"script_not_found: {CALENDAR_SCRIPT}")
    return script


def _argv(*args):
    return [
        _powershell(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(_script_path()),
        *args,
    ]


def _read_first_line(process, timeout):
    """First stdout line within `timeout`, or None.

    readline blocks, so it happens on a thread: the point of this whole module
    is not to block the caller for the length of the sign-in.
    """
    result = queue.Queue(maxsize=1)

    def reader():
        try:
            result.put(process.stdout.readline())
        except Exception as exc:  # the process died mid-read
            logger.warning("reading sign-in output failed: %s", exc)
            result.put("")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    try:
        line = result.get(timeout=timeout)
    except queue.Empty:
        return None

    return line.strip() or None


def _live_pending():
    """The in-flight sign-in, if there is one still worth offering."""
    if _pending is None:
        return None
    if _pending["process"].poll() is not None:
        return None  # the script exited; approved, failed, or expired
    if time.monotonic() >= _pending["expires_at"] - PENDING_EXPIRY_SKEW_SECONDS:
        return None
    return _pending["payload"]


def start_login(open_browser: bool = True) -> dict:
    """Start the device-code flow and return the code as soon as it is issued.

    The script keeps running after this returns: it polls until the code is
    approved or expires, then caches the tokens. Nothing here waits for that.

    Calling this again while a sign-in is already in flight returns the same
    code rather than starting another one, so repeated questions do not bury
    the user in browser tabs and codes.
    """
    if is_signed_in():
        # Signing in again would be harmless but pointless, and it would open a
        # browser tab at someone who did not ask for one.
        return {"stage": "signed-in", "already": True}

    with _pending_lock:
        existing = _live_pending()
        if existing is not None:
            logger.info("reusing the sign-in already awaiting approval")
            return existing
        return _spawn_login(open_browser)


def _spawn_login(open_browser: bool) -> dict:
    global _pending

    args = ["-Login"]
    if not open_browser:
        args.append("-NoBrowser")

    try:
        process = subprocess.Popen(
            _argv(*args),
            stdout=subprocess.PIPE,
            # The script's diagnostics are duplicated by the JSON stages, and an
            # unread stderr pipe is one more thing that could block the child.
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(task_registry.SCRIPTS_DIR),
        )
    except OSError as exc:
        raise CalendarAuthError(f"spawn_failed: {exc}") from exc

    line = _read_first_line(process, CODE_WAIT_SECONDS)
    if line is None:
        process.kill()
        raise CalendarAuthError("sign-in did not produce a code in time")

    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        process.kill()
        raise CalendarAuthError(f"unexpected sign-in output: {line[:200]}") from exc

    if payload.get("stage") == "failed":
        raise CalendarAuthError(payload.get("reason") or "sign-in failed")

    logger.info(
        "device code issued; waiting for approval (expires in %ss)",
        payload.get("expiresInSeconds"),
    )

    _pending = {
        "payload": payload,
        "process": process,
        "expires_at": time.monotonic() + int(payload.get("expiresInSeconds") or 900),
    }

    return payload


def sign_out() -> bool:
    """Delete the cached tokens. True when something was there to delete."""
    global _pending

    had_token = is_signed_in()

    with _pending_lock:
        if _pending is not None:
            # Whatever was mid-flight is moot now, and leaving it polling would
            # let it quietly re-create the tokens just deleted.
            if _pending["process"].poll() is None:
                _pending["process"].kill()
            _pending = None

    try:
        subprocess.run(
            _argv("-Logout"),
            capture_output=True,
            text=True,
            timeout=LOGOUT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            cwd=str(task_registry.SCRIPTS_DIR),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CalendarAuthError(f"sign-out failed: {exc}") from exc

    return had_token
