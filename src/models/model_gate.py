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
        self.audio_model_name = settings.audio_model_name
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

    def lookup(self, content: str) -> str:
        """Ask the model one small structured question and return the reply.

        Routing sampling, not speech sampling: matching a name or recalling a
        package identifier is a lookup, and the same question should give the
        same answer every time.
        """
        logger.info("requesting lookup from %s (%d chars)", self.model_name, len(content or ""))
        reply = self.helpers.chat(content)
        logger.debug("lookup reply: %s", reply)
        return reply

    def speak(self, content: str) -> str:
        """Turn a task result into one spoken line and return it.

        Same model as routing, different sampling: reading a result aloud is a
        rewrite, not a classification, so it uses the speech settings.
        """
        logger.info("requesting speech from %s (%d chars)", self.model_name, len(content or ""))
        reply = self.helpers.chat(
            content,
            temperature=settings.speech_temperature,
            max_tokens=settings.speech_max_tokens,
        )
        logger.debug("spoken reply: %s", reply)
        return reply.strip()

    def synthesize(self, text: str, output_path) -> str:
        """Render spoken text to a file on disk and return its path.

        The model loads on the first call, so a deployment that never asks for
        audio never pays for it.
        """
        logger.info("synthesising %d chars with %s", len(text or ""), self.audio_model_name)
        return self.helpers.generate_audio(self.helpers.audio_model(), text, output_path)
