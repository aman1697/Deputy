from src.config import Setting
from src.models.model_helpers import ModelHelpers
from src.utils.helpers import get_logger

logger = get_logger(__name__)

try:
    settings = Setting()
except Exception as exc:
    raise RuntimeError(
        f"Failed to load configuration (check .env for MODEL_NAME, HF_TOKEN): {exc}"
    ) from exc

helpers = ModelHelpers(settings)


class ModelGate:
    """The single entry point to the model. Nothing else calls it directly."""

    def __init__(self, model_name: str = None, model_helpers: ModelHelpers = None):
        self.model_name = model_name or settings.model_name
        self.helpers = model_helpers or helpers

    def route(self, content: str) -> str:
        """Send a routing prompt to the model and return the raw reply.

        Failures propagate as-is. `chat` already retries and wraps them with
        context, and the request handler logs the traceback once.
        """
        logger.info("requesting route from %s (%d chars)", self.model_name, len(content or ""))
        reply = self.helpers.chat(content)
        logger.debug("model reply: %s", reply)
        return reply
