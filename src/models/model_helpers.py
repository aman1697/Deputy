import time
from huggingface_hub import InferenceClient

from src.utils.constants import AUDIO_LANGUAGE, CUDA_CONFIG
from src.utils.helpers import get_logger

logger = get_logger(__name__)

RETRY_BACKOFF_SECONDS = 0.5


class ModelHelpers:
    def __init__(self, settings):
        self.settings = settings
        self._client = None
        self._audio_model = None

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
                provider=self.settings.model_provider or "auto",
                timeout=self.settings.request_timeout,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to initialize Hugging Face client: {exc}") from exc

        return self._client

    def _completion(self, content: str, multimodal: bool, temperature=None, max_tokens=None):
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
            model=self.settings.model_repo,
            messages=[{"role": self.settings.role, "content": message_content}],
            # None means "use the routing defaults", so a caller that needs
            # different sampling says so explicitly rather than mutating settings.
            temperature=self.settings.temperature if temperature is None else temperature,
            max_tokens=self.settings.max_tokens if max_tokens is None else max_tokens,
        )

        if not completion.choices:
            raise RuntimeError("Model returned no choices.")

        reply = completion.choices[0].message.content
        if not reply or not reply.strip():
            raise RuntimeError("Model returned an empty reply.")

        return reply

    def chat(self, content: str, multimodal: bool = True, temperature=None, max_tokens=None):
        """Run one completion, retrying transport-level failures.

        A retry is safe here: both routing and rephrasing are pure reads, so a
        repeated call has no side effects even if the first attempt actually
        reached the model.
        """
        attempts = max(1, self.settings.max_retries + 1)
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                return self._completion(content, multimodal, temperature, max_tokens)
            except ValueError:
                raise  # a bad payload will fail identically on every retry
            except Exception as exc:
                last_error = exc
                logger.warning("model call failed (attempt %s/%s): %s", attempt, attempts, exc)
                if attempt < attempts:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)

        raise RuntimeError(f"Model inference failed after {attempts} attempts: {last_error}")

    def audio_model(self):
        """Return a cached Qwen TTS model, loading it on first use.

        Loading is slow and needs a GPU, so it happens on the first request that
        actually wants speech rather than at import time. `qwen_tts` is imported
        here for the same reason: the rest of the service runs without it.
        """
        if self._audio_model is not None:
            return self._audio_model

        if not getattr(self.settings, "audio_model_name", None):
            raise ValueError("Missing 'audio_model_name' in settings.")

        try:
            from qwen_tts import Qwen3TTSModel

            self._audio_model = Qwen3TTSModel.from_pretrained(
                self.settings.audio_model_name,
                device_map=CUDA_CONFIG["device"],
                dtype=CUDA_CONFIG["dtype"],
                attn_implementation=CUDA_CONFIG["attn_implementation"],
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to load audio model: {exc}") from exc

        return self._audio_model

    def generate_audio(self, model, text: str, output_path: str):
        """Generate audio from text using the Qwen TTS model."""
        if not text or not text.strip():
            raise ValueError("Input 'text' cannot be empty.")
        if not output_path:
            raise ValueError("Output path cannot be empty.")

        try:
            import soundfile as sf

            wavs, sr = model.generate_voice_clone(
                text=text,
                language=AUDIO_LANGUAGE,
            )

            sf.write(str(output_path), wavs[0], sr)
        except Exception as exc:
            raise RuntimeError(f"Failed to generate audio: {exc}") from exc

        return output_path

