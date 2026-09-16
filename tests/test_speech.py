"""The speech half of the model layer: sampling, lazy loading, output paths."""

import pytest

from src.config import Setting
from src.models.model_gate import ModelGate
from src.models.model_helpers import ModelHelpers
from src.utils.constants import AUDIO_DIR, AUDIO_FORMAT, AUDIO_LANGUAGE
from src.utils.helpers import audio_output_path


class FakeHelpers:
    """Stands in for ModelHelpers without a network or a GPU."""

    def __init__(self, reply="Visual Studio Code.", audio_error=None):
        self.reply = reply
        self.audio_error = audio_error
        self.sampling = []
        self.load_count = 0
        self.synthesised = []
        self._model = object()

    def chat(self, content, multimodal=True, temperature=None, max_tokens=None):
        self.sampling.append((temperature, max_tokens))
        return self.reply

    def audio_model(self):
        self.load_count += 1
        return self._model

    def generate_audio(self, model, text, output_path):
        if self.audio_error:
            raise RuntimeError(self.audio_error)
        self.synthesised.append((model, text, output_path))
        return output_path


# ----------------------------------------------------------------------------
# ModelGate
# ----------------------------------------------------------------------------


def test_constructing_the_gate_does_not_load_the_tts_model():
    """Loading needs a GPU, so importing the service must not trigger it."""
    helpers = FakeHelpers()
    ModelGate(model_helpers=helpers)
    assert helpers.load_count == 0


def test_speak_uses_the_speech_sampling_settings():
    helpers = FakeHelpers()
    gate = ModelGate(model_helpers=helpers)
    settings = Setting()

    gate.speak("say this")

    assert helpers.sampling == [(settings.speech_temperature, settings.speech_max_tokens)]


def test_route_keeps_the_deterministic_routing_settings():
    """Routing is a classification: it must not inherit the speech temperature."""
    helpers = FakeHelpers()
    ModelGate(model_helpers=helpers).route("route this")

    assert helpers.sampling == [(None, None)]


def test_speak_strips_whitespace():
    """The reply is handed to a TTS model, which should not read padding."""
    gate = ModelGate(model_helpers=FakeHelpers(reply="  Visual Studio Code.\n\n"))
    assert gate.speak("p") == "Visual Studio Code."


def test_synthesize_passes_the_loaded_model_through():
    helpers = FakeHelpers()
    gate = ModelGate(model_helpers=helpers)

    returned = gate.synthesize("hello", "out.wav")

    assert returned == "out.wav"
    model, text, path = helpers.synthesised[0]
    assert (model, text, path) == (helpers._model, "hello", "out.wav")


def test_synthesize_propagates_failure():
    """serve.py decides how to degrade; the gate does not swallow the error."""
    gate = ModelGate(model_helpers=FakeHelpers(audio_error="no CUDA device"))

    with pytest.raises(RuntimeError, match="no CUDA device"):
        gate.synthesize("hello", "out.wav")


# ----------------------------------------------------------------------------
# ModelHelpers: sampling overrides and lazy loading
# ----------------------------------------------------------------------------


class FakeCompletions:
    def __init__(self):
        self.kwargs = []

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        message = type("M", (), {"content": "spoken line"})()
        choice = type("C", (), {"message": message})()
        return type("R", (), {"choices": [choice]})()


@pytest.fixture
def helpers_with_fake_client():
    helpers = ModelHelpers(Setting())
    completions = FakeCompletions()
    helpers._client = type("Client", (), {"chat": type("Chat", (), {})()})()
    helpers._client.chat.completions = completions
    return helpers, completions


@pytest.mark.parametrize(
    "model_id, repo, provider",
    [
        ("Qwen/Qwen3.8-27B:novita", "Qwen/Qwen3.8-27B", "novita"),
        ("Qwen/Qwen2.5-7B-Instruct", "Qwen/Qwen2.5-7B-Instruct", None),
    ],
)
def test_model_id_splits_the_provider_suffix(monkeypatch, model_id, repo, provider):
    """The hub rejects a colon inside `model=`; the provider is its own argument."""
    monkeypatch.setenv("MODEL_NAME", model_id)
    settings = Setting()

    assert settings.model_repo == repo
    assert settings.model_provider == provider


def test_chat_sends_the_repo_without_the_provider_suffix(helpers_with_fake_client):
    helpers, completions = helpers_with_fake_client

    helpers.chat("hi")

    assert completions.kwargs[0]["model"] == helpers.settings.model_repo
    assert ":" not in completions.kwargs[0]["model"]


def test_chat_defaults_to_the_settings(helpers_with_fake_client):
    helpers, completions = helpers_with_fake_client
    settings = Setting()

    helpers.chat("hi")

    sent = completions.kwargs[0]
    assert sent["temperature"] == settings.temperature
    assert sent["max_tokens"] == settings.max_tokens


def test_chat_honours_an_override(helpers_with_fake_client):
    helpers, completions = helpers_with_fake_client

    helpers.chat("hi", temperature=0.4, max_tokens=512)

    sent = completions.kwargs[0]
    assert sent["temperature"] == 0.4
    assert sent["max_tokens"] == 512


def test_a_zero_override_is_not_mistaken_for_absent(helpers_with_fake_client):
    """0.0 is falsy but a legitimate temperature, so `is None` is the only test."""
    helpers, completions = helpers_with_fake_client

    helpers.chat("hi", temperature=0.0, max_tokens=1)

    assert completions.kwargs[0]["temperature"] == 0.0
    assert completions.kwargs[0]["max_tokens"] == 1


def test_audio_model_is_loaded_once_and_cached(monkeypatch):
    loads = []

    class FakeTTS:
        @classmethod
        def from_pretrained(cls, name, **kwargs):
            loads.append((name, kwargs))
            return cls()

    monkeypatch.setitem(__import__("sys").modules, "qwen_tts", _module_with(FakeTTS))
    helpers = ModelHelpers(Setting())

    first, second = helpers.audio_model(), helpers.audio_model()

    assert first is second
    assert len(loads) == 1
    assert loads[0][0] == Setting().audio_model_name


def test_generate_audio_writes_the_file_and_returns_its_path(monkeypatch, tmp_path):
    written = {}

    class FakeModel:
        def generate_voice_clone(self, text, language):
            written["language"] = language
            return ([[0.0, 0.1]], 16_000)

    monkeypatch.setitem(
        __import__("sys").modules,
        "soundfile",
        _module_with(None, write=lambda path, data, sr: written.update(path=path, sr=sr)),
    )
    helpers = ModelHelpers(Setting())
    target = tmp_path / f"spoken.{AUDIO_FORMAT}"

    returned = helpers.generate_audio(FakeModel(), "hello there", target)

    assert returned == target
    assert written["path"] == str(target)
    assert written["sr"] == 16_000
    assert written["language"] == AUDIO_LANGUAGE


@pytest.mark.parametrize(
    "text, output_path",
    [("   ", "out.wav"), ("", "out.wav"), ("hello", "")],
)
def test_generate_audio_rejects_empty_input(text, output_path):
    with pytest.raises(ValueError):
        ModelHelpers(Setting()).generate_audio(object(), text, output_path)


# ----------------------------------------------------------------------------
# Output paths
# ----------------------------------------------------------------------------


def test_audio_path_carries_the_request_id():
    path = audio_output_path("abc123")

    assert path.parent == AUDIO_DIR
    assert path.name.endswith(f"-abc123.{AUDIO_FORMAT}")


def test_audio_path_sanitises_the_request_id():
    """request_id can reach us from a model reply, so it cannot shape a path."""
    path = audio_output_path("../../etc/pwn")

    assert path.parent == AUDIO_DIR
    assert ".." not in path.name
    assert "/" not in path.name


@pytest.mark.parametrize("request_id", [None, "", "   ", "///"])
def test_audio_path_stays_unique_without_a_usable_id(request_id):
    first = audio_output_path(request_id)
    second = audio_output_path(request_id)

    assert first != second
    assert first.suffix == f".{AUDIO_FORMAT}"


def test_audio_path_creates_the_directory():
    assert audio_output_path("abc123").parent.is_dir()


def _module_with(tts_class, **attrs):
    """Build a throwaway module object for monkeypatching an optional import."""
    import types

    module = types.ModuleType("fake")
    if tts_class is not None:
        module.Qwen3TTSModel = tts_class
    for name, value in attrs.items():
        setattr(module, name, value)
    return module
