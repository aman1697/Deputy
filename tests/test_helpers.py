import pytest

from src.utils.helpers import extract_json_object

ROUTE = {"task_id": "healthcheck", "args": []}


@pytest.mark.parametrize(
    "raw",
    [
        '{"task_id": "healthcheck", "args": []}',
        '  {"task_id": "healthcheck", "args": []}  ',
        '```json\n{"task_id": "healthcheck", "args": []}\n```',
        '```\n{"task_id": "healthcheck", "args": []}\n```',
        'Sure! {"task_id": "healthcheck", "args": []}',
        '{"task_id": "healthcheck", "args": []} — hope that helps.',
        'Here you go:\n{"task_id": "healthcheck", "args": []}\nLet me know.',
    ],
)
def test_recovers_object_from_untidy_model_output(raw):
    assert extract_json_object(raw) == ROUTE


def test_handles_nested_objects():
    payload = extract_json_object('prefix {"a": {"b": {"c": 1}}} suffix')
    assert payload == {"a": {"b": {"c": 1}}}


def test_braces_inside_strings_do_not_confuse_the_scanner():
    payload = extract_json_object('{"note": "a } brace and a \\" quote"}')
    assert payload == {"note": 'a } brace and a " quote'}


def test_skips_unparseable_leading_brace():
    assert extract_json_object('{not json} then {"ok": true}') == {"ok": True}


@pytest.mark.parametrize("raw", ["", "no json here", "{unclosed", "[1, 2, 3]", None, 42])
def test_returns_none_when_there_is_no_object(raw):
    assert extract_json_object(raw) is None
