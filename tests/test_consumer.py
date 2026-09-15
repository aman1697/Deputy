import pytest

from src.consumer.consume import MAX_MESSAGE_BYTES, Consumer

consumer = Consumer()


def test_consumes_the_router_contract():
    reply = consumer.consume('{"task_id": "healthcheck", "args": []}')
    assert reply["ok"]
    assert reply["result"]["output"] == "ok"
    assert reply["error"] is None


def test_consumes_a_verbatim_catalog_echo():
    """The shape the model was emitting when routing silently did nothing."""
    reply = consumer.consume('{"id":"healthcheck","type":"noop","executor":"-"}')
    assert reply["ok"]


def test_consumes_fenced_output():
    reply = consumer.consume('```json\n{"task_id":"host-info"}\n```')
    assert reply["ok"]
    assert "system" in reply["result"]["output"]


def test_consumes_a_dict_directly():
    assert consumer.consume({"task_id": "healthcheck"})["ok"]


def test_request_id_is_echoed_back():
    reply = consumer.consume({"task_id": "healthcheck", "request_id": "abc-123"})
    assert reply["request_id"] == "abc-123"


def test_handled_ms_is_recorded():
    assert consumer.consume({"task_id": "healthcheck"})["handled_ms"] >= 0


@pytest.mark.parametrize(
    "message,error",
    [
        ("not json at all", "malformed_json"),
        ("[1,2,3]", "payload_not_object"),
        ('{"task_id": "unknown-task"}', "validation_failed"),
        ('{"nothing": true}', "validation_failed"),
        (12345, "unsupported_message_type"),
        (b"\xff\xfe invalid", "invalid_encoding"),
    ],
)
def test_bad_messages_are_rejected_cleanly(message, error):
    reply = consumer.consume(message)
    assert not reply["ok"]
    assert reply["error"] == error


def test_oversized_messages_are_rejected():
    # The payload is built inside the test rather than parametrized: a 64KB
    # test id overflows the env var pytest records the current test in.
    oversized = "x" * (MAX_MESSAGE_BYTES + 1)
    assert consumer.consume(oversized)["error"] == "message_too_large"
    assert consumer.consume(oversized.encode())["error"] == "message_too_large"


def test_missing_script_is_not_retryable():
    reply = consumer.consume({"task_id": "collect-logs"})
    assert not reply["ok"]
    assert reply["error"] == "script_not_found"
    assert reply["retryable"] is False


def test_environment_failures_are_retryable():
    class Stub:
        def executor(self, task):
            return {
                "ok": False,
                "task_id": "x",
                "task_type": "powershell",
                "exit_code": None,
                "stdout": "",
                "output": "",
                "stderr": "",
                "error": "timeout",
                "duration_ms": 1,
            }

    reply = Consumer(runner=Stub()).consume({"task_id": "healthcheck"})
    assert reply["retryable"] is True


def test_a_crashing_runner_does_not_kill_the_consumer():
    class Boom:
        def executor(self, task):
            raise ValueError("kaboom")

    reply = Consumer(runner=Boom()).consume({"task_id": "healthcheck"})
    assert not reply["ok"]
    assert reply["error"] == "runner_crash: ValueError"
