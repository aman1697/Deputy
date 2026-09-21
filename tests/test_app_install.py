"""Launching apps, and the offer-then-install exchange.

Installing software is the one thing here that changes the machine, so most of
these tests are about the paths that must *not* install anything.
"""

import json
import shutil

import pytest
from fastapi.testclient import TestClient

import serve
from src.executor import app_install
from src.executor.task_registry import catalog, get_entry
from src.executor.task_runner import TaskRunner

needs_powershell = pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None,
    reason="PowerShell is not available on this machine",
)


@pytest.fixture(autouse=True)
def no_leftover_offer():
    """Module state: an offer from one test must not arm the next."""
    app_install.clear()
    yield
    app_install.clear()


# ------------------------------------------------------------- consent rules --


@pytest.mark.parametrize(
    "reply",
    ["yes", "Yes", "YES", "yeah", "yep", "sure", "ok", "okay", "go ahead", "do it",
     "yes please", "install it", "y", "Yes!", "yes.", " yes  "],
)
def test_clear_agreement_is_accepted(reply):
    assert app_install.is_affirmative(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "no",
        "nope",
        "not now",
        "no thanks",
        "maybe later",
        "",
        "   ",
        None,
        "what is it",
        # The dangerous class: a "yes" that is not an answer to our question.
        "yes what time is my next meeting",
        "did you say yes",
        "yesterday's logs please",
        "open blender",
    ],
)
def test_anything_short_of_a_clear_yes_is_refused(reply):
    assert not app_install.is_affirmative(reply)


# ------------------------------------------------------------ package id rules --


@pytest.mark.parametrize(
    "package_id",
    ["Microsoft.VisualStudioCode", "VideoLAN.VLC", "BlenderFoundation.Blender", "7zip.7zip"],
)
def test_plausible_package_ids_pass(package_id):
    assert app_install.is_valid_package_id(package_id)


@pytest.mark.parametrize(
    "package_id",
    [
        None,
        "",
        "   ",
        "has space",
        "winget install --id X",      # a command, not an id
        "Foo.Bar; rm -rf /",          # injection attempt
        "Foo.Bar && calc",
        "Foo|Bar",
        '"Foo.Bar"',
        "-Foo.Bar",                   # would read as a flag
        "x" * 200,                    # unbounded length
    ],
)
def test_implausible_package_ids_are_refused(package_id):
    assert not app_install.is_valid_package_id(package_id)


def test_an_offer_can_only_be_taken_once():
    """One yes installs one thing."""
    app_install.offer("Blender", "BlenderFoundation.Blender")

    assert app_install.take_offer()["app_name"] == "Blender"
    assert app_install.take_offer() is None


def test_an_expired_offer_is_not_honoured(monkeypatch):
    monkeypatch.setattr(app_install, "OFFER_TTL_SECONDS", -1)
    app_install.offer("Blender", "BlenderFoundation.Blender")

    assert app_install.take_offer() is None


# ------------------------------------------------------------------ arg rules --


def test_an_app_name_is_admitted_by_pattern():
    entry = get_entry("launch-app")
    assert TaskRunner._extract_args({"args": ["-Name", "Visual Studio Code"]}, entry) == [
        "-Name",
        "Visual Studio Code",
    ]


@pytest.mark.parametrize(
    "args",
    [
        ["-Name", "app; rm -rf /"],
        ["-Name", "app && calc"],
        ["-Name", "app|calc"],
        ["-Name", '"app"'],
        ["-Name", "$(calc)"],
        ["-Name", "app\x00"],
        ["-Name", "-SomeFlag"],        # would read as a flag, not a name
        ["-Name", "x" * 60],           # past the pattern's length cap
        ["-Unexpected", "app"],
    ],
)
def test_shell_shaped_app_names_are_refused(args):
    assert TaskRunner._extract_args({"args": args}, get_entry("launch-app")) is None


def test_a_pattern_does_not_loosen_tasks_that_have_none():
    """Adding args_pattern must not weaken the existing allowlist-only tasks."""
    assert TaskRunner._extract_args({"args": ["anything"]}, get_entry("get-calendar")) is None


def test_install_is_hidden_from_the_router():
    """The model must never be able to choose to install software."""
    assert "install-app" not in {task["task_id"] for task in catalog()}
    assert get_entry("install-app") is not None  # but the service can still run it


def test_launch_is_routable():
    assert "launch-app" in {task["task_id"] for task in catalog()}


# ------------------------------------------------------------------ the script --


@needs_powershell
def test_launching_reports_a_missing_app_with_an_inventory():
    runner = TaskRunner()
    result = runner.executor(
        {"task_id": "launch-app", "args": ["-Name", "definitelynotinstalled", "-Check"]}
    )

    assert result["error"] == "app_not_installed"
    payload = json.loads(result["output"])
    assert payload["installed"] is False
    assert isinstance(payload["candidates"], list)
    # The inventory rides along so a caller can resolve names the script cannot.
    assert isinstance(payload["inventory"], list)
    assert payload["inventory"]


@needs_powershell
def test_checking_an_installed_app_does_not_launch_it():
    runner = TaskRunner()
    result = runner.executor({"task_id": "launch-app", "args": ["-Name", "notepad", "-Check"]})

    if not result["ok"]:
        pytest.skip("notepad not found on this machine")

    payload = json.loads(result["output"])
    assert payload["installed"] is True
    assert payload["launched"] is False


# ------------------------------------------------------------------- the flow --


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(serve.settings, "audio_enabled", False)
    monkeypatch.setattr(serve.model_gate, "speak", lambda prompt: "stub speech")
    return TestClient(serve.app)


def _missing_app_execution(monkeypatch, requested="blender", inventory=None):
    payload = {
        "requested": requested,
        "matched": None,
        "installed": False,
        "launched": False,
        "candidates": [],
        "inventory": inventory if inventory is not None else ["Notepad++", "Visual Studio Code"],
    }
    monkeypatch.setattr(
        serve.consumer,
        "consume",
        lambda message, request_id=None: {
            "request_id": request_id,
            "ok": False,
            "error": "app_not_installed",
            "retryable": False,
            "result": {"task_id": "launch-app", "output": json.dumps(payload), "stderr": ""},
            "handled_ms": 1,
        },
    )


def test_a_missing_app_becomes_an_offer(client, monkeypatch):
    monkeypatch.setattr(serve.model_gate, "route", lambda p: '{"task_id":"launch-app","args":[]}')
    _missing_app_execution(monkeypatch)
    monkeypatch.setattr(
        serve.model_gate,
        "lookup",
        lambda prompt: '{"match":null}'
        if "INSTALLED APPLICATIONS" in prompt
        else '{"package_id":"BlenderFoundation.Blender"}',
    )

    body = client.post("/active_window", json={"content": "open blender"}).json()

    assert "don't have blender installed" in body["spoken_text"].lower()
    assert "say yes" in body["spoken_text"].lower()
    assert app_install.take_offer()["package_id"] == "BlenderFoundation.Blender"


def test_an_unknown_package_is_admitted_not_guessed(client, monkeypatch):
    monkeypatch.setattr(serve.model_gate, "route", lambda p: '{"task_id":"launch-app","args":[]}')
    _missing_app_execution(monkeypatch, requested="some bespoke tool")
    monkeypatch.setattr(
        serve.model_gate,
        "lookup",
        lambda prompt: '{"match":null}'
        if "INSTALLED APPLICATIONS" in prompt
        else '{"package_id":null}',
    )

    body = client.post("/active_window", json={"content": "open some bespoke tool"}).json()

    assert "couldn't work out which package" in body["spoken_text"]
    assert app_install.take_offer() is None


def test_an_invented_package_id_is_refused(client, monkeypatch):
    """A model returning a command instead of an id must not reach winget."""
    monkeypatch.setattr(serve.model_gate, "route", lambda p: '{"task_id":"launch-app","args":[]}')
    _missing_app_execution(monkeypatch)
    monkeypatch.setattr(
        serve.model_gate,
        "lookup",
        lambda prompt: '{"match":null}'
        if "INSTALLED APPLICATIONS" in prompt
        else '{"package_id":"winget install --id Foo; calc"}',
    )

    body = client.post("/active_window", json={"content": "open blender"}).json()

    assert "couldn't work out which package" in body["spoken_text"]
    assert app_install.take_offer() is None


def test_an_alias_resolves_and_launches_instead_of_offering(client, monkeypatch):
    """'vs code' is installed as 'Visual Studio Code'; that is a launch, not an offer."""
    monkeypatch.setattr(serve.model_gate, "route", lambda p: '{"task_id":"launch-app","args":[]}')

    calls = []

    def consume(message, request_id=None):
        calls.append(json.loads(message) if isinstance(message, str) else message)
        if len(calls) == 1:
            return {
                "request_id": request_id,
                "ok": False,
                "error": "app_not_installed",
                "retryable": False,
                "result": {
                    "task_id": "launch-app",
                    "output": json.dumps(
                        {"requested": "vs code", "installed": False,
                         "inventory": ["Visual Studio Code", "Notepad++"]}
                    ),
                    "stderr": "",
                },
                "handled_ms": 1,
            }
        return {
            "request_id": request_id,
            "ok": True,
            "error": None,
            "retryable": False,
            "result": {
                "task_id": "launch-app",
                "output": json.dumps({"matched": "Visual Studio Code", "launched": True}),
                "stderr": "",
            },
            "handled_ms": 1,
        }

    monkeypatch.setattr(serve.consumer, "consume", consume)
    monkeypatch.setattr(serve.model_gate, "lookup", lambda p: '{"match":"Visual Studio Code"}')

    body = client.post("/active_window", json={"content": "open vs code"}).json()

    assert body["ok"] is True
    assert calls[1]["args"] == ["-Name", "Visual Studio Code"]
    assert app_install.take_offer() is None  # nothing was offered; it just opened


def test_a_hallucinated_match_is_not_launched(client, monkeypatch):
    """Only a name the machine actually reported may be launched."""
    monkeypatch.setattr(serve.model_gate, "route", lambda p: '{"task_id":"launch-app","args":[]}')
    _missing_app_execution(monkeypatch, inventory=["Notepad++"])
    monkeypatch.setattr(
        serve.model_gate,
        "lookup",
        lambda prompt: '{"match":"Some App That Is Not Installed"}'
        if "INSTALLED APPLICATIONS" in prompt
        else '{"package_id":"BlenderFoundation.Blender"}',
    )

    body = client.post("/active_window", json={"content": "open blender"}).json()

    # Fell through to the install offer rather than launching a phantom.
    assert "say yes" in body["spoken_text"].lower()


def test_yes_installs_the_offered_package(client, monkeypatch):
    app_install.offer("Blender", "BlenderFoundation.Blender")

    sent = {}

    def consume(message, request_id=None):
        sent["payload"] = json.loads(message)
        return {
            "request_id": request_id,
            "ok": True,
            "error": None,
            "retryable": False,
            "result": {"task_id": "install-app", "output": "{}", "stderr": ""},
            "handled_ms": 1,
        }

    monkeypatch.setattr(serve.consumer, "consume", consume)

    body = client.post("/active_window", json={"content": "yes"}).json()

    assert sent["payload"]["task_id"] == "install-app"
    assert sent["payload"]["args"] == ["-PackageId", "BlenderFoundation.Blender"]
    assert sent["payload"]["timeout"] == serve.INSTALL_TIMEOUT_SECONDS
    assert "Blender is installed now" in body["spoken_text"]


@pytest.mark.parametrize(
    "error, expected",
    [
        ("winget_not_available", "package manager"),
        ("package_not_found", "no package by that name"),
        ("install_failed", "didn't finish"),
    ],
)
def test_a_failed_install_says_why(client, monkeypatch, error, expected):
    app_install.offer("Blender", "BlenderFoundation.Blender")
    monkeypatch.setattr(
        serve.consumer,
        "consume",
        lambda message, request_id=None: {
            "request_id": request_id,
            "ok": False,
            "error": error,
            "retryable": False,
            "result": {"task_id": "install-app", "output": "{}", "stderr": ""},
            "handled_ms": 1,
        },
    )

    body = client.post("/active_window", json={"content": "yes"}).json()

    assert body["ok"] is False
    assert expected in body["spoken_text"]


def test_yes_with_no_offer_is_just_a_question(client, monkeypatch):
    """Nothing pending means "yes" routes normally rather than installing."""
    monkeypatch.setattr(serve.model_gate, "route", lambda p: '{"task_id":"healthcheck","args":[]}')

    body = client.post("/active_window", json={"content": "yes"}).json()

    assert body["task_id"] == "healthcheck"


def test_a_new_question_does_not_consume_a_standing_offer(client, monkeypatch):
    """Asking something else must not be read as agreeing to install."""
    app_install.offer("Blender", "BlenderFoundation.Blender")
    monkeypatch.setattr(serve.model_gate, "route", lambda p: '{"task_id":"healthcheck","args":[]}')

    def consume(message, request_id=None):
        assert "install-app" not in message
        return {
            "request_id": request_id,
            "ok": True,
            "error": None,
            "retryable": False,
            "result": {"task_id": "healthcheck", "output": "ok", "stderr": ""},
            "handled_ms": 1,
        }

    monkeypatch.setattr(serve.consumer, "consume", consume)

    body = client.post("/active_window", json={"content": "are you there"}).json()

    assert body["task_id"] == "healthcheck"
    # Still pending: declining by changing the subject should not cancel it.
    assert app_install.take_offer() is not None
