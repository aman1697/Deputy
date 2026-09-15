from src.executor import task_registry


class TaskValidator:
    """Turns an untrusted payload into a trusted registry entry, or nothing."""

    @staticmethod
    def normalize(data):
        """Return (task_id, task_type) from a payload, or (None, None).

        `task_id` is the only field the caller must supply. `task_type` is
        optional: the registry already knows how to run a task, so a payload
        that omits it is fine, and one that supplies it is cross-checked.

        Both key spellings are accepted. The router model replies with
        "task_id"; queue messages and older catalog-echo replies use "id"/
        "type".
        """
        if not isinstance(data, dict):
            return None, None

        task_id = data.get("task_id", data.get("id"))
        if not isinstance(task_id, str) or not task_id.strip():
            return None, None

        task_type = data.get("task_type", data.get("type"))
        if isinstance(task_type, str) and task_type.strip():
            task_type = task_type.strip()
        else:
            task_type = None

        return task_id.strip(), task_type

    @classmethod
    def validate_task(cls, data):
        return cls.resolve(data) is not None

    @classmethod
    def resolve(cls, data):
        """Return the registry entry for a valid payload, else None."""
        task_id, task_type = cls.normalize(data)
        if task_id is None:
            return None

        entry = task_registry.get_entry(task_id)
        if entry is None:
            return None

        # A supplied type must agree with the registry. Disagreement means the
        # caller thinks it is running something other than what we would run.
        if task_type is not None and entry["type"] != task_type:
            return None

        return entry
