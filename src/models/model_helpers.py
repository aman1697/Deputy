import time

from huggingface_hub import InferenceClient

from src.utils.helpers import get_logger

logger = get_logger(__name__)

RETRY_BACKOFF_SECONDS = 0.5


class ModelHelpers:
    def __init__(self, settings):
        self.settings = settings
        self._client = None

    def get_hf_client(self):
        """Return a cached client.

        The client holds a connection pool, so rebuilding it per request threw
        away pooling and paid for a fresh TLS handshake every time.
        """
        if self._client is not None:
            return self._client

        if not getattr(self.settings, "hf_token", None):
            raise ValueError("Missing 'hf_token' in settings.")

        try:
            self._client = InferenceClient(
                api_key=self.settings.hf_token,
                timeout=self.settings.request_timeout,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to initialize Hugging Face client: {exc}") from exc

        return self._client

    def _completion(self, content: str, multimodal: bool):
        if not content or not content.strip():
            raise ValueError("Input 'content' cannot be empty.")
        if not getattr(self.settings, "model_name", None):
            raise ValueError("Missing 'model_name' in settings.")
        if not getattr(self.settings, "role", None):
            raise ValueError("Missing 'role' in settings.")

        message_content = (
            [{"type": "text", "text": content}] if multimodal else content
        )

        completion = self.get_hf_client().chat.completions.create(
            model=self.settings.model_name,
            messages=[{"role": self.settings.role, "content": message_content}],
            temperature=self.settings.temperature,
            max_tokens=self.settings.max_tokens,
        )

        if not completion.choices:
            raise RuntimeError("Model returned no choices.")

        reply = completion.choices[0].message.content
        if not reply or not reply.strip():
            raise RuntimeError("Model returned an empty reply.")

        return reply

    def chat(self, content: str, multimodal: bool = True):
        """Run one completion, retrying transport-level failures.

        A retry is safe here: routing is a pure read, so a repeated call has no
        side effects even if the first attempt actually reached the model.
        """
        attempts = max(1, self.settings.max_retries + 1)
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                return self._completion(content, multimodal)
            except ValueError:
                raise  # a bad payload will fail identically on every retry
            except Exception as exc:
                last_error = exc
                logger.warning("model call failed (attempt %s/%s): %s", attempt, attempts, exc)
                if attempt < attempts:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)

        raise RuntimeError(f"Model inference failed after {attempts} attempts: {last_error}")
