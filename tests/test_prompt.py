import json

from src.ingestor.prompt_ingestor import PromptIngestor
from src.prompts.router_prompt import FALLBACK_TASK_ID
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
