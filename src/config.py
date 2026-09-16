from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Setting(BaseSettings):
    """Configuration settings for the application."""

    model_name: str = Field(validation_alias=AliasChoices("MODEL_NAME", "MODEL_ID"))
    audio_model_name: str = Field(validation_alias=AliasChoices("AUDIO_MODEL_NAME", "AUDIO_MODEL_ID"))
    hf_token: str
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

    # Synthesis needs a GPU and the TTS weights. Off by default is wrong for the
    # intended deployment, but this lets a dev box return spoken text without one.
    audio_enabled: bool = True

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

    @property
    def model_repo(self) -> str:
        """The model id with any provider suffix removed."""
        return self.model_name.partition(":")[0]

    @property
    def model_provider(self) -> str | None:
        """The provider named in MODEL_ID, or None to let the hub choose."""
        return self.model_name.partition(":")[2] or None
