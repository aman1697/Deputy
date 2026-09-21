from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Setting(BaseSettings):
    """Configuration settings for the application."""

    model_name: str = Field(validation_alias=AliasChoices("MODEL_NAME", "MODEL_ID"))
    audio_model_name: str = Field(validation_alias=AliasChoices("AUDIO_MODEL_NAME", "AUDIO_MODEL_ID"))

    # Point at any OpenAI-compatible endpoint - Ollama on localhost, Groq, or
    # anything else - and Hugging Face is bypassed entirely. Unset means use
    # Hugging Face inference providers as before.
    model_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MODEL_BASE_URL", "OPENAI_BASE_URL"),
    )

    # Not required when model_base_url points somewhere that needs no key, such
    # as a local Ollama. The client checks for it only on the Hugging Face path.
    hf_token: str = ""
    role: str = "user"

    # Routing is a classification, not a creative task: a fixed temperature
    # keeps the same query mapping to the same task run after run.
    temperature: float = 0.0
    # The reply is a single small JSON object. Capping this bounds both latency
    # and the damage a model that starts rambling can do.
    max_tokens: int = 256

    # The spoken rephrase is the opposite of routing: it needs enough freedom to
    # sound like a person, and a couple of sentences of room to say it in.
    speech_temperature: float = 0.4
    speech_max_tokens: int = 512

    # Synthesis needs a CUDA GPU plus flash-attn, which the current machine does
    # not have, so it is off until someone turns it on. The spoken *text* is
    # produced either way; this only controls whether a .wav is rendered.
    # Set AUDIO_ENABLED=true once the GPU side is available.
    audio_enabled: bool = False

    # Without a timeout a stalled inference call holds the request open
    # indefinitely and ties up a worker thread.
    request_timeout: int = 30
    max_retries: int = 2

    # Loopback by default: this service runs local scripts on the host, so
    # binding every interface would hand that to anyone on the network. Set
    # HOST=0.0.0.0 deliberately if you need it reachable.
    host: str = "127.0.0.1"
    port: int = 5000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # MODEL_ID may carry an inference provider as a "<repo>:<provider>" suffix,
    # e.g. Qwen/Qwen3.8-27B:novita. huggingface_hub rejects that colon inside
    # `model=` — the provider is a separate argument to InferenceClient — so it
    # is split here rather than at each call site.
    #
    # That split must not happen for a custom endpoint: an Ollama model is named
    # "qwen2.5:7b-instruct", where the colon is part of the name. Splitting it
    # would ask for a model "qwen2.5" from a provider "7b-instruct".

    @property
    def model_repo(self) -> str:
        """The model id as the configured endpoint expects it."""
        if self.model_base_url:
            return self.model_name
        return self.model_name.partition(":")[0]

    @property
    def model_provider(self) -> str | None:
        """The provider named in MODEL_ID, or None when there isn't one."""
        if self.model_base_url:
            return None
        return self.model_name.partition(":")[2] or None
