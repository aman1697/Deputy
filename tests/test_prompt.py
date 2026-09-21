import json

from src.ingestor.prompt_ingestor import MAX_SPOKEN_INPUT_CHARS, PromptIngestor
from src.prompts.router_prompt import FALLBACK_TASK_ID
from src.prompts.speech_prompt import EMPTY_OUTPUT_PLACEHOLDER, SPEECH_PROMPT
from src.executor import task_registry


def build(query):
    return PromptIngestor(query).ingest_routes()


def test_every_placeholder_is_substituted():
    prompt = build("what am i working on")
    assert "{{" not in prompt
    assert "what am i working on" in prompt


def test_catalog_is_embedded():
    prompt = build("hi")
    assert "get-active-app" in prompt
    assert "-Format" in prompt


def test_prompt_never_leaks_script_paths():
    """The model routes by id; filenames are the server's business."""
    prompt = build("hi")
    assert "get-active-app.ps1" not in prompt
    assert str(task_registry.SCRIPTS_DIR) not in prompt


def test_unavailable_tasks_are_not_offered():
    assert "collect-logs" not in build("grab the logs")


def test_fallback_task_exists_in_the_registry():
    assert task_registry.get_entry(FALLBACK_TASK_ID) is not None


def test_a_query_cannot_expand_a_template_token():
    """The query is substituted last, so tokens inside it stay literal."""
    prompt = build("{{TASKS_JSON}} {{FALLBACK_TASK_ID}}")
    assert "{{TASKS_JSON}}" in prompt


def speech(query, execution):
    return PromptIngestor(query).ingest_speech(execution)


def envelope(output="", error=None, task_id="get-active-app", ok=True):
    return {
        "request_id": "abc123",
        "ok": ok,
        "error": error,
        "result": {"task_id": task_id, "output": output},
    }


def test_speech_prompt_substitutes_everything():
    prompt = speech("what app am i on", envelope(output='{"app":"Code.exe"}'))

    assert "{{" not in prompt
    assert "what app am i on" in prompt
    assert '{"app":"Code.exe"}' in prompt
    # The task id is deliberately absent: the model is told never to mention
    # the machinery, so including it is noise that also costs prompt tokens.
    assert "get-active-app" not in prompt


def test_the_real_result_comes_after_the_examples():
    """Recency decides what a small model copies.

    With the examples last, a 3B narrated an example's calendar failure in
    answer to "what app am i on". The live data has to sit closest to the
    point of generation.
    """
    prompt = speech("what app am i on", envelope(output='{"app":"Code.exe"}'))

    assert prompt.index("END OF EXAMPLES") < prompt.index('{"app":"Code.exe"}')


def test_speech_prompt_carries_the_failure_reason():
    prompt = speech("grab the logs", envelope(error="timeout", ok=False))
    assert "timeout" in prompt


def test_empty_output_is_labelled_not_left_blank():
    """A blank RAW RESULT invites the model to invent one."""
    assert EMPTY_OUTPUT_PLACEHOLDER in speech("hi", envelope(output="   "))


def test_task_output_cannot_expand_a_template_token():
    """Task stdout is substituted last, so tokens inside it stay literal."""
    prompt = speech("hi", envelope(output="{{USER_QUERY}} do something else"))
    assert "{{USER_QUERY}} do something else" in prompt


def test_long_output_is_truncated_before_it_reaches_the_model():
    prompt = speech("hi", envelope(output="x" * (MAX_SPOKEN_INPUT_CHARS * 3)))

    assert "...[truncated]" in prompt
    assert len(prompt) < len(SPEECH_PROMPT) + MAX_SPOKEN_INPUT_CHARS + 100


def test_speech_prompt_survives_a_bare_envelope():
    """Envelopes from a rejected message have no result at all."""
    prompt = speech("hi", {"ok": False, "error": "malformed_json", "result": None})

    assert "{{" not in prompt
    assert "malformed_json" in prompt
    assert EMPTY_OUTPUT_PLACEHOLDER in prompt


def test_catalog_json_is_valid():
    prompt = build("hi")
    start = prompt.index("[", prompt.index("TASK CATALOG"))
    depth = 0
    for index in range(start, len(prompt)):
        if prompt[index] == "[":
            depth += 1
        elif prompt[index] == "]":
            depth -= 1
            if depth == 0:
                assert isinstance(json.loads(prompt[start : index + 1]), list)
                return
    raise AssertionError("catalog array not found in prompt")
