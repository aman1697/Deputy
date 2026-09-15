from src.executor import task_registry
from src.utils.helpers import get_logger

logger = get_logger(__name__)


class Ingestor:
    """Supplies the task context the router prompt is built from."""

    @staticmethod
    def ingest_tasks(available_only=True):
        """Return the router-facing task catalog.

        Raises if the catalog is empty. Routing against an empty catalog does
        not fail visibly, it silently sends every query to the fallback task,
        which looks like a working agent that has stopped doing anything.
        """
        catalog = task_registry.catalog(available_only=available_only)
        if not catalog:
            raise RuntimeError(
                f"Task catalog is empty; check {task_registry.TASKS_FILE}"
            )
        return catalog

    @staticmethod
    def unavailable_tasks():
        """Registered tasks whose script is not on disk yet."""
        return [
            entry["id"]
            for entry in task_registry.load().values()
            if not task_registry.is_available(entry)
        ]
