import pytest
from fastapi.testclient import TestClient

import serve


@pytest.fixture
def client(monkeypatch):
    """A client whose model replies with a canned route, so no network is used."""

    def stub(reply):
        monkeypatch.setattr(serve.model_gate, "route", lambda prompt: reply)

    client = TestClient(serve.app)
    client.stub_model = stub
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
