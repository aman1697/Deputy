"""Speech-to-text. Runs on CPU by design, unlike the TTS side of this project.

`faster-whisper` decodes whatever audio a browser's MediaRecorder produces
(webm/opus, ogg, wav) using its own bundled decoder (`av`), so no system
ffmpeg install is required.
"""

from src.utils.constants import STT_MODEL_DIR
from src.utils.helpers import get_logger

logger = get_logger(__name__)

# A device name faster-whisper accepts directly; kept as a constant rather than
# threaded through settings because this project has no GPU path for it yet.
DEVICE = "cpu"
COMPUTE_TYPE = "int8"  # fastest accurate-enough option on CPU

# Below this many bytes, a "recording" is very likely silence or a mic glitch,
# not a real phrase. Transcribing it anyway just returns empty text slowly.
MIN_AUDIO_BYTES = 2_000


class SpeechToText:
    def __init__(self, settings):
        self.settings = settings
        self._model = None

    def model(self):
        """Return a cached faster-whisper model, loading it on first use.

        Loading reads a few hundred MB from disk; the first request after
        startup pays for that, nothing after does.
        """
        if self._model is not None:
            return self._model

        try:
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model

            STT_MODEL_DIR.mkdir(parents=True, exist_ok=True)

            # WhisperModel's own `download_root` uses huggingface_hub's default
            # cache layout, which symlinks blobs into place - something this
            # machine's account cannot do without Developer Mode or an admin
            # shell, and it fails outright rather than falling back. Resolving
            # the path ourselves via `output_dir` copies the files instead.
            model_dir = download_model(
                self.settings.stt_model_size,
                output_dir=str(STT_MODEL_DIR / self.settings.stt_model_size),
            )
            self._model = WhisperModel(model_dir, device=DEVICE, compute_type=COMPUTE_TYPE)
        except Exception as exc:
            raise RuntimeError(f"Failed to load speech-to-text model: {exc}") from exc

        return self._model

    def transcribe(self, audio_bytes: bytes) -> str:
        """Return the spoken text in `audio_bytes`, or raise.

        Takes raw bytes rather than a path: the caller has an upload in
        memory, and faster-whisper decodes a file-like object directly, so
        there is no reason to round-trip through a temp file.
        """
        if not audio_bytes or len(audio_bytes) < MIN_AUDIO_BYTES:
            raise ValueError("Recording is empty or too short to transcribe.")

        import io

        buffer = io.BytesIO(audio_bytes)

        try:
            segments, _info = self.model().transcribe(buffer, language="en")
            text = "".join(segment.text for segment in segments).strip()
        except Exception as exc:
            raise RuntimeError(f"Transcription failed: {exc}") from exc

        if not text:
            raise ValueError("Could not make out any words in that recording.")

        return text
