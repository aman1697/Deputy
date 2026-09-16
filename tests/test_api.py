import pytest
from fastapi.testclient import TestClient

import serve
from src.prompts.speech_prompt import SPEECH_FALLBACK


SPOKEN = "Here's what I found."


@pytest.fixture
def client(monkeypatch):
    """A client whose model replies with a canned route, so no network is used."""

    def stub(reply):
        monkeypatch.setattr(serve.model_gate, "route", lambda prompt: reply)

    def stub_speech(reply):
        """Takes a canned string, or a callable to inspect the prompt or raise."""
        speak = reply if callable(reply) else lambda prompt: reply
        monkeypatch.setattr(serve.model_gate, "speak", speak)

    def stub_audio(path):
        monkeypatch.setattr(serve.settings, "audio_enabled", True)
        monkeypatch.setattr(serve.model_gate, "synthesize", lambda text, out: path)

    # Every request now makes a second model call. Stubbed by default so a test
    # that only cares about routing still cannot reach the network.
    stub_speech(SPOKEN)

    client = TestClient(serve.app)
    client.stub_model = stub
    client.stub_speech = stub_speech
    client.stub_audio = stub_audio
    return client


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_tasks_lists_the_catalog(client):
    body = client.get("/tasks").json()
    ids = {task["task_id"] for task in body["available"]}

    assert "get-active-app" in ids
    assert "healthcheck" in ids
    # Registered but not yet implemented, so it is surfaced separately rather
    # than offered to the router.
    assert "collect-logs" in body["unavailable"]
    assert "collect-logs" not in ids


def test_tasks_reports_allowed_args(client):
    body = client.get("/tasks").json()
    task = next(t for t in body["available"] if t["task_id"] == "get-active-app")
    assert "-Format" in task["allowed_args"]


def test_route_and_run(client):
    client.stub_model('{"task_id": "host-info", "args": []}')
    body = client.post("/active_window", json={"content": "what os am i on"}).json()

    assert body["ok"] is True
    assert body["task_id"] == "host-info"
    assert "system" in body["output"]
    assert body["error"] is None
    assert body["execution"]["result"]["exit_code"] == 0


def test_untidy_model_output_still_runs(client):
    client.stub_model('Sure!\n```json\n{"task_id":"healthcheck"}\n```')
    body = client.post("/active_window", json={"content": "you up?"}).json()
    assert body["ok"] is True
    assert body["output"] == "ok"


def test_unroutable_reply_reports_failure_not_500(client):
    client.stub_model('{"task_id": "totally-made-up"}')
    response = client.post("/active_window", json={"content": "hi"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "validation_failed"
    # The raw reply is kept so a bad route can be debugged after the fact.
    assert "totally-made-up" in body["routing_reply"]


@pytest.mark.parametrize("content", ["", "   "])
def test_empty_content_is_a_400(client, content):
    response = client.post("/active_window", json={"content": content})
    assert response.status_code == 400


def test_missing_content_is_a_422(client):
    assert client.post("/active_window", json={}).status_code == 422


def test_model_failure_is_a_500(client):
    def boom(prompt):
        raise RuntimeError("model unreachable")

    serve.model_gate.route = boom
    try:
        response = client.post("/active_window", json={"content": "hi"})
        assert response.status_code == 500
        assert "model unreachable" in response.json()["detail"]
    finally:
        del serve.model_gate.route


# ----------------------------------------------------------------------------
# The spoken stage
# ----------------------------------------------------------------------------


def test_result_is_rephrased_for_speech(client):
    client.stub_model('{"task_id": "healthcheck", "args": []}')
    body = client.post("/active_window", json={"content": "you up?"}).json()

    assert body["spoken_text"] == SPOKEN
    # The raw output is still returned alongside it; speech does not replace it.
    assert body["output"] == "ok"


def test_speech_sees_the_execution_result(client):
    """What reaches the speech model is the task's own output, not the route."""
    seen = {}

    def capture(prompt):
        seen["prompt"] = prompt
        return SPOKEN

    client.stub_model('{"task_id": "host-info", "args": []}')
    client.stub_speech(capture)
    client.post("/active_window", json={"content": "what os am i on"})

    assert "host-info" in seen["prompt"]
    assert "system" in seen["prompt"]


def test_a_failed_task_is_still_spoken(client):
    client.stub_model('{"task_id": "totally-made-up"}')
    body = client.post("/active_window", json={"content": "hi"}).json()

    assert body["ok"] is False
    assert body["spoken_text"] == SPOKEN


def test_speech_failure_does_not_lose_the_result(client):
    """The task already ran. A broken rephrase must not turn that into a 500."""

    def boom(prompt):
        raise RuntimeError("inference 503")

    client.stub_model('{"task_id": "healthcheck", "args": []}')
    client.stub_speech(boom)
    response = client.post("/active_window", json={"content": "you up?"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["output"] == "ok"
    assert body["spoken_text"] == SPEECH_FALLBACK


# ----------------------------------------------------------------------------
# Synthesis
# ----------------------------------------------------------------------------


def test_audio_path_is_returned_when_enabled(client):
    client.stub_model('{"task_id": "healthcheck", "args": []}')
    client.stub_audio("C:/audio/spoken.wav")
    body = client.post("/active_window", json={"content": "you up?"}).json()

    assert body["audio_path"] == "C:/audio/spoken.wav"


def test_no_audio_when_disabled(client, monkeypatch):
    """Disabled means the model is never touched, not that the call is caught."""
    calls = []
    monkeypatch.setattr(serve.model_gate, "synthesize", lambda text, out: calls.append(out))

    client.stub_model('{"task_id": "healthcheck", "args": []}')
    body = client.post("/active_window", json={"content": "you up?"}).json()

    assert calls == []
    assert body["audio_path"] is None
    assert body["spoken_text"] == SPOKEN


def test_synthesis_failure_keeps_the_spoken_text(client, monkeypatch):
    def boom(text, out):
        raise RuntimeError("no CUDA device")

    client.stub_model('{"task_id": "healthcheck", "args": []}')
    monkeypatch.setattr(serve.settings, "audio_enabled", True)
    monkeypatch.setattr(serve.model_gate, "synthesize", boom)
    response = client.post("/active_window", json={"content": "you up?"})

    assert response.status_code == 200
    body = response.json()
    assert body["audio_path"] is None
    assert body["spoken_text"] == SPOKEN


def test_nothing_to_say_means_no_audio(client):
    client.stub_model('{"task_id": "healthcheck", "args": []}')
    client.stub_speech("   ")
    client.stub_audio("C:/audio/spoken.wav")
    body = client.post("/active_window", json={"content": "you up?"}).json()

    assert body["audio_path"] is None


# ----------------------------------------------------------------------------
# request_id
# ----------------------------------------------------------------------------


def test_request_id_is_server_generated_and_threaded_through(client):
    client.stub_model('{"task_id": "healthcheck", "args": []}')
    body = client.post("/active_window", json={"content": "you up?"}).json()

    assert body["request_id"]
    # One id for the whole request: the envelope must carry the same one.
    assert body["execution"]["request_id"] == body["request_id"]


def test_each_request_gets_its_own_id(client):
    client.stub_model('{"task_id": "healthcheck", "args": []}')
    first = client.post("/active_window", json={"content": "you up?"}).json()
    second = client.post("/active_window", json={"content": "you up?"}).json()

    assert first["request_id"] != second["request_id"]


def test_a_model_supplied_request_id_cannot_override_ours(client):
    """request_id names the audio file, so the model must not get to choose it."""
    client.stub_model('{"task_id": "healthcheck", "args": [], "request_id": "../../pwn"}')
    body = client.post("/active_window", json={"content": "you up?"}).json()

    assert body["execution"]["request_id"] == body["request_id"] != "../../pwn"
