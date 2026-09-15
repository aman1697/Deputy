import pytest

from src.executor.task_validator import TaskValidator

validator = TaskValidator()


def test_accepts_the_router_contract():
    entry = validator.resolve({"task_id": "get-active-app", "args": []})
    assert entry["id"] == "get-active-app"
    assert entry["type"] == "powershell"
    assert entry["executor"] == "get-active-app.ps1"


def test_accepts_a_verbatim_catalog_echo():
    """Older prompts had the model echo the whole entry; still supported."""
    entry = validator.resolve(
        {"id": "get-active-app", "type": "powershell", "executor": "get-active-app.ps1"}
    )
    assert entry["id"] == "get-active-app"


def test_task_id_alone_is_enough():
    assert validator.resolve({"task_id": "healthcheck"})["type"] == "noop"


def test_declared_type_must_match_the_registry():
    assert validator.resolve({"task_id": "get-active-app", "task_type": "noop"}) is None


def test_executor_is_not_accepted_as_a_type():
    """The original bug: executor was compared against task_type."""
    assert validator.resolve(
        {"task_id": "get-active-app", "task_type": "get-active-app.ps1"}
    ) is None


def test_whitespace_is_tolerated():
    assert validator.resolve({"task_id": "  healthcheck  "})["id"] == "healthcheck"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"task_id": ""},
        {"task_id": "   "},
        {"task_id": "does-not-exist"},
        {"task_id": 42},
        {"task_id": None},
        {"id": ["healthcheck"]},
        "not a dict",
        None,
    ],
)
def test_rejects_bad_payloads(payload):
    assert validator.resolve(payload) is None
    assert validator.validate_task(payload) is False


def test_an_unregistered_id_cannot_smuggle_in_an_executor():
    payload = {"task_id": "evil", "type": "powershell", "executor": "get-active-app.ps1"}
    assert validator.resolve(payload) is None
