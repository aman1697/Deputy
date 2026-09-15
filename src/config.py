from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Setting(BaseSettings):
    """Configuration settings for the application."""

    model_name: str = Field(validation_alias=AliasChoices("MODEL_NAME", "MODEL_ID"))
    hf_token: str
    role: str = "user"

    # Routing is a classification, not a creative task: a fixed temperature
    # keeps the same query mapping to the same task run after run.
    temperature: float = 0.0
    # The reply is a single small JSON object. Capping this bounds both latency
    # and the damage a model that starts rambling can do.
    max_tokens: int = 256

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
