"""The calendar sign-in flow: exit-code naming, code extraction, endpoints."""

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

import serve
from src.executor import calendar_auth
from src.executor.task_registry import get_entry
from src.executor.task_runner import TaskRunner


# ----------------------------------------------------------- exit code names --


@pytest.mark.parametrize(
    "exit_code, expected",
    [
        (4, "not_signed_in_to_calendar"),
        (1, "calendar_service_unreachable"),
        (2, "calendar_response_unreadable"),
    ],
)
def test_declared_exit_codes_become_named_reasons(exit_code, expected):
    """'task_failed: exit 4' tells the user nothing they can act on."""
    assert TaskRunner._exit_error(get_entry("get-calendar"), exit_code) == expected


def test_undeclared_exit_codes_keep_the_generic_reason():
    assert TaskRunner._exit_error(get_entry("get-calendar"), 9) == "task_failed: exit 9"


def test_tasks_without_an_error_map_are_unaffected():
    assert TaskRunner._exit_error(get_entry("get-active-app"), 1) == "task_failed: exit 1"


def test_a_signed_out_calendar_query_reports_the_named_reason():
    """End to end through the runner, so the envelope carries the actionable name."""
    runner = TaskRunner()
    result = runner.executor({"task_id": "get-calendar", "args": ["-Window", "today"]})

    if result["ok"]:
        pytest.skip("calendar is signed in on this machine")
    assert result["error"] in {
        "not_signed_in_to_calendar",
        "calendar_service_unreachable",
        "calendar_response_unreadable",
        "powershell_not_available",
        "script_not_found",
    }


# --------------------------------------------------------------- code parsing --


@pytest.fixture(autouse=True)
def no_leftover_pending_login():
    """`_pending` is module state; a test that set it must not leak into the next."""
    calendar_auth._pending = None
    yield
    calendar_auth._pending = None


class FakeProcess:
    """Stands in for the PowerShell sign-in process."""

    def __init__(self, first_line):
        self.stdout = self
        self._line = first_line
        self.killed = False
        self.returncode = None  # None means "still polling"

    def readline(self):
        return self._line

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.fixture
def fake_spawn(monkeypatch):
    """Replace Popen so no PowerShell runs and no browser opens."""
    spawned = {}

    def install(first_line):
        def popen(argv, **kwargs):
            spawned["argv"] = argv
            spawned["process"] = FakeProcess(first_line)
            return spawned["process"]

        monkeypatch.setattr(subprocess, "Popen", popen)
        monkeypatch.setattr(calendar_auth, "is_signed_in", lambda: False)
        return spawned

    return install


PENDING_LINE = json.dumps(
    {
        "stage": "pending",
        "userCode": "ANY2ALT87",
        "verificationUri": "https://login.microsoft.com/device",
        "expiresInSeconds": 900,
        "browserOpened": True,
    }
)


def test_start_login_returns_the_code_without_waiting(fake_spawn):
    fake_spawn(PENDING_LINE)

    payload = calendar_auth.start_login()

    assert payload["stage"] == "pending"
    assert payload["userCode"] == "ANY2ALT87"


def test_start_login_passes_no_browser_through(fake_spawn):
    spawned = fake_spawn(PENDING_LINE)

    calendar_auth.start_login(open_browser=False)

    assert "-NoBrowser" in spawned["argv"]


def test_start_login_omits_no_browser_by_default(fake_spawn):
    spawned = fake_spawn(PENDING_LINE)

    calendar_auth.start_login()

    assert "-NoBrowser" not in spawned["argv"]
    assert "-Login" in spawned["argv"]


def test_start_login_is_a_no_op_when_already_signed_in(monkeypatch):
    """Re-running it would open a browser tab at someone who didn't ask."""
    monkeypatch.setattr(calendar_auth, "is_signed_in", lambda: True)

    def explode(*args, **kwargs):
        raise AssertionError("must not spawn a sign-in when already signed in")

    monkeypatch.setattr(subprocess, "Popen", explode)

    assert calendar_auth.start_login() == {"stage": "signed-in", "already": True}


def test_a_failed_stage_raises(fake_spawn):
    fake_spawn(json.dumps({"stage": "failed", "reason": "access_denied"}))

    with pytest.raises(calendar_auth.CalendarAuthError, match="access_denied"):
        calendar_auth.start_login()


def test_unparseable_output_raises_and_kills_the_process(fake_spawn):
    spawned = fake_spawn("not json at all")

    with pytest.raises(calendar_auth.CalendarAuthError, match="unexpected sign-in output"):
        calendar_auth.start_login()

    assert spawned["process"].killed


def test_no_output_in_time_raises_rather_than_hanging(fake_spawn, monkeypatch):
    spawned = fake_spawn("")
    monkeypatch.setattr(calendar_auth, "CODE_WAIT_SECONDS", 0.2)

    with pytest.raises(calendar_auth.CalendarAuthError, match="did not produce a code"):
        calendar_auth.start_login()

    assert spawned["process"].killed


# ------------------------------------------------------------------ endpoints --


@pytest.fixture
def client():
    return TestClient(serve.app)


def test_status_reports_signed_out(client, monkeypatch):
    monkeypatch.setattr(calendar_auth, "is_signed_in", lambda: False)
    assert client.get("/calendar/status").json() == {"signed_in": False}


def test_status_reports_signed_in(client, monkeypatch):
    monkeypatch.setattr(calendar_auth, "is_signed_in", lambda: True)
    assert client.get("/calendar/status").json() == {"signed_in": True}


def test_login_endpoint_returns_the_code_and_instructions(client, monkeypatch):
    monkeypatch.setattr(
        calendar_auth, "start_login", lambda open_browser=True: json.loads(PENDING_LINE)
    )

    body = client.post("/calendar/login").json()

    assert body["stage"] == "pending"
    assert body["user_code"] == "ANY2ALT87"
    assert body["verification_uri"] == "https://login.microsoft.com/device"
    assert body["browser_opened"] is True
    assert "ANY2ALT87" in body["message"]


def test_login_endpoint_reports_already_signed_in(client, monkeypatch):
    monkeypatch.setattr(
        calendar_auth,
        "start_login",
        lambda open_browser=True: {"stage": "signed-in", "already": True},
    )

    body = client.post("/calendar/login").json()

    assert body["stage"] == "signed-in"
    assert body["user_code"] is None


def test_login_failure_is_a_500_with_a_reason(client, monkeypatch):
    def boom(open_browser=True):
        raise calendar_auth.CalendarAuthError("powershell_not_available")

    monkeypatch.setattr(calendar_auth, "start_login", boom)

    response = client.post("/calendar/login")

    assert response.status_code == 500
    assert "powershell_not_available" in response.json()["detail"]


def test_logout_reports_the_resulting_state(client, monkeypatch):
    monkeypatch.setattr(calendar_auth, "sign_out", lambda: True)
    monkeypatch.setattr(calendar_auth, "is_signed_in", lambda: False)

    assert client.post("/calendar/logout").json() == {"signed_in": False}


def test_login_is_not_a_routable_task():
    """Everything the model can choose is read-only; sign-in is user-triggered."""
    from src.executor import task_registry

    ids = {task["task_id"] for task in task_registry.catalog()}
    assert not {"calendar-login", "get-calendar-login"} & ids


# ------------------------------------------------- sign-in inside a question --


@pytest.fixture
def asking_client(monkeypatch):
    """A client whose router always picks the calendar, with both LLMs stubbed."""
    monkeypatch.setattr(
        serve.model_gate, "route", lambda prompt: '{"task_id":"get-calendar","args":[]}'
    )
    monkeypatch.setattr(serve.model_gate, "speak", lambda prompt: "stub speech")
    monkeypatch.setattr(serve.settings, "audio_enabled", False)
    return TestClient(serve.app)


def _signed_out_execution(monkeypatch):
    """Make the calendar task report that nobody has signed in."""
    monkeypatch.setattr(
        serve.consumer,
        "consume",
        lambda message, request_id=None: {
            "request_id": request_id,
            "ok": False,
            "error": "not_signed_in_to_calendar",
            "retryable": False,
            "result": {"task_id": "get-calendar", "output": "", "stderr": ""},
            "handled_ms": 1,
        },
    )


def test_a_meeting_question_while_signed_out_starts_sign_in_and_says_the_code(
    asking_client, monkeypatch
):
    """The whole point: the code reaches the user through the answer."""
    _signed_out_execution(monkeypatch)
    monkeypatch.setattr(
        calendar_auth, "start_login", lambda: json.loads(PENDING_LINE)
    )

    body = asking_client.post(
        "/active_window", json={"content": "do i have any meeting today"}
    ).json()

    assert body["auth_required"] is True
    assert body["user_code"] == "ANY2ALT87"
    assert body["verification_uri"] == "https://login.microsoft.com/device"
    # Spelled out, because a nine-character code read as a word is unusable.
    assert "A-N-Y-2-A-L-T-8-7" in body["spoken_text"]


def test_the_spoken_code_is_not_left_to_the_model(asking_client, monkeypatch):
    """A paraphrased auth code is a broken auth code, so the model never sees it."""
    _signed_out_execution(monkeypatch)
    monkeypatch.setattr(calendar_auth, "start_login", lambda: json.loads(PENDING_LINE))

    def explode(prompt):
        raise AssertionError("the speech model must not be asked to relay the code")

    monkeypatch.setattr(serve.model_gate, "speak", explode)

    body = asking_client.post("/active_window", json={"content": "any meetings?"}).json()

    assert "A-N-Y-2-A-L-T-8-7" in body["spoken_text"]


def test_sign_in_failure_still_answers_conversationally(asking_client, monkeypatch):
    _signed_out_execution(monkeypatch)

    def boom():
        raise calendar_auth.CalendarAuthError("powershell_not_available")

    monkeypatch.setattr(calendar_auth, "start_login", boom)

    body = asking_client.post("/active_window", json={"content": "any meetings?"}).json()

    assert body["auth_required"] is True
    assert body["user_code"] is None
    assert "sign-in" in body["spoken_text"]


def test_a_normal_question_never_mentions_signing_in(asking_client, monkeypatch):
    """Only the not-signed-in error triggers this path."""
    monkeypatch.setattr(
        serve.consumer,
        "consume",
        lambda message, request_id=None: {
            "request_id": request_id,
            "ok": True,
            "error": None,
            "retryable": False,
            "result": {"task_id": "get-calendar", "output": '{"eventCount":0}', "stderr": ""},
            "handled_ms": 1,
        },
    )

    def explode():
        raise AssertionError("must not start a sign-in for a working query")

    monkeypatch.setattr(calendar_auth, "start_login", explode)

    body = asking_client.post("/active_window", json={"content": "any meetings?"}).json()

    assert body["auth_required"] is False
    assert body["user_code"] is None
    assert body["spoken_text"] == "stub speech"


def test_a_sign_in_that_completed_meanwhile_does_not_invent_a_code(
    asking_client, monkeypatch
):
    _signed_out_execution(monkeypatch)
    monkeypatch.setattr(
        calendar_auth, "start_login", lambda: {"stage": "signed-in", "already": True}
    )

    body = asking_client.post("/active_window", json={"content": "any meetings?"}).json()

    assert body["user_code"] is None
    assert "ask me again" in body["spoken_text"].lower()


def test_asking_twice_reuses_the_same_code(monkeypatch, fake_spawn):
    """Two questions must not mean two browser tabs and two codes."""
    spawned = fake_spawn(PENDING_LINE)

    first = calendar_auth.start_login()
    spawned["process"].returncode = None  # still polling
    second = calendar_auth.start_login()

    assert first["userCode"] == second["userCode"]
