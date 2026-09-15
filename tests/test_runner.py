import shutil
import sys
import time

import pytest

from src.executor.task_registry import get_entry
from src.executor.task_runner import MAX_ARGS, TaskRunner

runner = TaskRunner()

needs_powershell = pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="PowerShell is not available on this machine",
)


# --------------------------------------------------------------- arg policy --


def test_args_are_rejected_for_tasks_that_do_not_opt_in():
    assert TaskRunner._extract_args({"args": ["-x"]}, get_entry("host-info")) is None


def test_empty_args_are_always_fine():
    entry = get_entry("host-info")
    assert TaskRunner._extract_args({}, entry) == []
    assert TaskRunner._extract_args({"args": []}, entry) == []
    assert TaskRunner._extract_args({"args": None}, entry) == []


def test_allowlisted_args_pass():
    entry = get_entry("get-active-app")
    assert TaskRunner._extract_args({"args": ["-Format", "json"]}, entry) == [
        "-Format",
        "json",
    ]


@pytest.mark.parametrize(
    "args",
    [
        ["-Watch"],                      # would hang until the timeout
        ["-IdleThresholdSeconds", "0"],  # takes a free-form value
        ["; Remove-Item C:\\"],          # injection attempt
        ["-Format", "yaml"],             # plausible but not permitted
        ["-format", "json"],             # allowlist is case-sensitive
        ["-Format\x00", "json"],         # embedded NUL
        [1, 2],                          # not strings
        "not-a-list",
    ],
)
def test_args_outside_the_allowlist_are_refused(args):
    assert TaskRunner._extract_args({"args": args}, get_entry("get-active-app")) is None


def test_arg_count_is_bounded():
    entry = get_entry("get-active-app")
    assert TaskRunner._extract_args({"args": ["-Format"] * (MAX_ARGS + 1)}, entry) is None


# ----------------------------------------------------------------- timeouts --


@pytest.mark.parametrize(
    "raw,expected", [(5, 5), (900, 900), (0, 60), (-1, 60), (901, 60), (True, 60), ("5", 60), (None, 60)]
)
def test_timeout_is_clamped(raw, expected):
    assert TaskRunner._extract_timeout({"timeout": raw}) == expected


# ------------------------------------------------------------------ running --


def test_noop_runs():
    result = runner.executor({"task_id": "healthcheck"})
    assert result["ok"] and result["output"] == "ok"


def test_builtin_runs():
    result = runner.executor({"task_id": "host-info"})
    assert result["ok"]
    assert "system" in result["output"]


def test_missing_script_is_reported_not_crashed():
    result = runner.executor({"task_id": "collect-logs"})
    assert not result["ok"]
    assert result["error"] == "script_not_found"


def test_invalid_task_is_reported():
    result = runner.executor({"task_id": "nope"})
    assert not result["ok"]
    assert result["error"] == "validation_failed"


def test_nonzero_exit_reports_a_reason():
    """ok=false with error=null would leave the caller guessing."""
    argv = [sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"]
    result = runner._run_process(get_entry("host-info"), argv, 30, time.monotonic())

    assert not result["ok"]
    assert result["exit_code"] == 3
    assert result["error"] == "task_failed: exit 3"
    assert result["stderr"] == "bad"


def test_zero_exit_reports_no_error():
    argv = [sys.executable, "-c", "print('fine')"]
    result = runner._run_process(get_entry("host-info"), argv, 30, time.monotonic())

    assert result["ok"]
    assert result["error"] is None
    assert result["output"] == "fine"


def test_timeout_is_reported_as_transient():
    argv = [sys.executable, "-c", "import time; time.sleep(10)"]
    result = runner._run_process(get_entry("host-info"), argv, 1, time.monotonic())
    assert result["error"] == "timeout"


def test_a_script_reading_stdin_does_not_hang():
    """stdin is closed, so a blocking read returns EOF instead of deadlocking."""
    argv = [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"]
    result = runner._run_process(get_entry("host-info"), argv, 10, time.monotonic())
    assert result["ok"]


@needs_powershell
def test_powershell_task_runs_and_returns_a_single_line():
    result = runner.executor({"task_id": "get-active-app"})
    assert result["ok"], result
    assert result["exit_code"] == 0
    assert result["output"]
    assert "\n" not in result["output"]
    # `output` is the trimmed form of stdout, which keeps its newline.
    assert result["stdout"].strip() == result["output"]


@needs_powershell
def test_powershell_task_honours_allowlisted_format_arg():
    import json

    result = runner.executor({"task_id": "get-active-app", "args": ["-Format", "json"]})
    assert result["ok"], result
    payload = json.loads(result["output"])
    assert {"timestamp", "app", "pid", "idleSeconds", "title"} <= set(payload)
