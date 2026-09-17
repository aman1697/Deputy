from src.executor import task_registry


def test_catalog_hides_executor_from_the_model():
    """The model picks an id; it must never see or choose a filename."""
    for task in task_registry.catalog():
        assert "executor" not in task
        assert "task_id" in task
        assert task["description"], f"{task['task_id']} needs a description to route on"


def test_catalog_exposes_allowlisted_args_only():
    entry = task_registry.get_entry("get-active-app")
    task = next(t for t in task_registry.catalog() if t["task_id"] == "get-active-app")
    assert task["allowed_args"] == list(entry["args_allowlist"])


def test_tasks_without_args_advertise_none():
    task = next(t for t in task_registry.catalog() if t["task_id"] == "host-info")
    assert "allowed_args" not in task


def test_available_only_hides_missing_scripts():
    ids = {t["task_id"] for t in task_registry.catalog(available_only=True)}
    all_ids = {t["task_id"] for t in task_registry.catalog()}

    # collect_logs.py is not written yet, so it is registered but not runnable.
    assert "collect-logs" in all_ids
    assert "collect-logs" not in ids
    assert "get-active-app" in ids


def test_builtin_and_noop_are_always_available():
    for task_id in ("host-info", "healthcheck"):
        assert task_registry.is_available(task_registry.get_entry(task_id))


def test_resolve_script_refuses_traversal():
    assert task_registry.resolve_script("../../../etc/passwd") is None
    assert task_registry.resolve_script("..\\..\\serve.py") is None
    assert task_registry.resolve_script("get-active-app.ps1") is not None


def test_comment_key_is_not_parsed_as_a_task():
    assert "_comment" not in task_registry.load()


def test_calendar_task_is_registered_and_runnable():
    entry = task_registry.get_entry("get-calendar")

    assert entry["type"] == "powershell"
    assert task_registry.is_available(entry)
    assert "get-calendar" in {t["task_id"] for t in task_registry.catalog(available_only=True)}


def test_calendar_allowlist_covers_every_window_and_nothing_else():
    """The router can choose a window; it cannot reach the other parameters."""
    allowed = set(task_registry.get_entry("get-calendar")["args_allowlist"])

    assert allowed == {"-Window", "next3h", "today", "next24h", "week"}
    # Deliberately absent: -IncludeAttendees would ship colleague names to the
    # inference provider, and -MaxEvents takes a free-form value.
    assert "-IncludeAttendees" not in allowed
    assert "-MaxEvents" not in allowed
