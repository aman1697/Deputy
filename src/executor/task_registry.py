import json
from functools import lru_cache
from pathlib import Path

# Imported rather than recomputed, and re-exported so callers can keep using
# task_registry.SCRIPTS_DIR / .TASKS_FILE.
from src.utils.constants import SCRIPTS_DIR, TASKS_FILE

# Used only when a registry entry omits an explicit "type".
TYPE_BY_SUFFIX = {".ps1": "powershell", ".py": "python"}

# Types whose executor names a file in SCRIPTS_DIR. Everything else runs
# in-process, so there is nothing on disk to look for.
SCRIPT_TYPES = frozenset({"powershell", "python"})


def _clean_string_list(raw):
    """Normalize a JSON list of strings, dropping blanks. Non-lists give ()."""
    if not isinstance(raw, list):
        return ()
    return tuple(
        item.strip() for item in raw if isinstance(item, str) and item.strip()
    )


def _build_entry(task_id, executor, task_type, task):
    return {
        "id": task_id,
        "type": task_type,
        "executor": executor,
        "description": (task.get("description") or "").strip(),
        "allow_args": task.get("allow_args") is True,
        # When present, an arg must appear verbatim in this list. It is the
        # difference between "this task takes flags" and "this task takes
        # arbitrary attacker-chosen flags".
        "args_allowlist": _clean_string_list(task.get("args_allowlist")),
    }


def _parse_tasks_list(tasks):
    """Current format: {"tasks": [{"id": ..., "executor": ...}, ...]}."""
    entries = {}

    for task in tasks:
        if not isinstance(task, dict):
            continue

        task_id = task.get("id")
        executor = task.get("executor")
        if not isinstance(task_id, str) or not isinstance(executor, str):
            continue

        task_id = task_id.strip()
        executor = executor.strip()
        if not task_id or not executor:
            continue

        task_type = task.get("type")
        if isinstance(task_type, str) and task_type.strip():
            task_type = task_type.strip()
        else:
            task_type = TYPE_BY_SUFFIX.get(Path(executor).suffix.lower())

        if not task_type:
            continue

        entries[task_id] = _build_entry(task_id, executor, task_type, task)

    return entries


def _parse_legacy(registry, entries):
    """Legacy format: {"task_name": "script.ps1"}."""
    for key, value in registry.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        key = key.strip()
        value = value.strip()
        if not key or not value or key in entries:
            continue
        task_type = TYPE_BY_SUFFIX.get(Path(value).suffix.lower())
        if not task_type:
            continue
        entries[key] = _build_entry(key, value, task_type, {})
    return entries


@lru_cache(maxsize=4)
def _parse(path, stamp):
    """Parse tasks.json into {task_id: entry}.

    `stamp` is unused in the body; it is part of the cache key so the cached
    result is dropped when the file changes on disk.
    """
    try:
        with open(path, "r", encoding="utf-8") as file:
            registry = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(registry, dict):
        return {}

    tasks = registry.get("tasks")
    entries = _parse_tasks_list(tasks) if isinstance(tasks, list) else {}
    return _parse_legacy(registry, entries)


def load():
    """Stat the file, then hand a cache-invalidating stamp to the parser."""
    try:
        status = TASKS_FILE.stat()
    except OSError:
        return {}
    return _parse(str(TASKS_FILE), (status.st_mtime_ns, status.st_size))


def get_entry(task_id):
    return load().get(task_id)


def resolve_script(name):
    """Resolve a registered script name inside SCRIPTS_DIR, or return None.

    Rejects anything that escapes SCRIPTS_DIR, so a malformed or tampered
    registry entry like "../../etc/passwd" cannot be reached.
    """
    try:
        candidate = (SCRIPTS_DIR / name).resolve()
    except OSError:
        return None
    if not candidate.is_relative_to(SCRIPTS_DIR):
        return None
    if not candidate.is_file():
        return None
    return candidate


def is_available(entry):
    """True when this task could actually run right now.

    Script-backed tasks are unavailable until their file lands in SCRIPTS_DIR,
    which lets the catalog carry a task before its script is written without
    the router offering something that can only fail.
    """
    if entry["type"] not in SCRIPT_TYPES:
        return True
    return resolve_script(entry["executor"]) is not None


def catalog(available_only=False):
    """The task list as the router model should see it.

    Deliberately omits `executor`: the model picks an id and the registry
    resolves how to run it, so the model never needs a filename and can never
    redirect us to a different one.
    """
    view = []

    for entry in sorted(load().values(), key=lambda item: item["id"]):
        if available_only and not is_available(entry):
            continue

        item = {"task_id": entry["id"], "description": entry["description"]}
        if entry["allow_args"] and entry["args_allowlist"]:
            item["allowed_args"] = list(entry["args_allowlist"])
        view.append(item)

    return view
