from __future__ import annotations

import json

from src.ingestor.context_ingest import Ingestor
from src.prompts.app_prompt import PACKAGE_ID_PROMPT, RESOLVE_APP_PROMPT
from src.prompts.router_prompt import FALLBACK_TASK_ID, ROUTER_PROMPT
from src.prompts.speech_prompt import EMPTY_OUTPUT_PLACEHOLDER, SPEECH_PROMPT

# The spoken line is a sentence or two. Handing the model 64k of stdout to
# summarise costs tokens and latency for text that will never be read out.
MAX_SPOKEN_INPUT_CHARS = 4_000


class PromptIngestor:
    def __init__(self, input_query: str):
        self.ingestor = Ingestor()
        self.query = input_query

    def ingest_routes(self) -> str:
        try:
            tasks_json = json.dumps(
                self.ingestor.ingest_tasks(), ensure_ascii=True, indent=2
            )

            # Explicit token replacement, because the prompt is full of literal
            # JSON braces that str.format would try to interpret. Order matters:
            # the query is substituted last, so a query containing a token name
            # is inert rather than being expanded.
            return (
                ROUTER_PROMPT.replace("{{TASKS_JSON}}", tasks_json)
                .replace("{{FALLBACK_TASK_ID}}", FALLBACK_TASK_ID)
                .replace("{{INPUT_QUERY}}", self.query)
            )
        except Exception as e:
            raise RuntimeError(f"Error ingesting route prompt: {e}") from e

    def ingest_speech(self, execution: dict) -> str:
        """Build the prompt that turns an execution envelope into a spoken line.

        Takes the envelope `Consumer.consume` returns, so the caller does not
        have to know which fields live on the envelope and which on the result.
        """
        try:
            result = (execution or {}).get("result") or {}

            output = (result.get("output") or "").strip()
            if len(output) > MAX_SPOKEN_INPUT_CHARS:
                output = output[:MAX_SPOKEN_INPUT_CHARS] + "\n...[truncated]"

            # Same ordering rule as ingest_routes: everything model-controlled or
            # script-controlled is substituted last, so a token name appearing in
            # stdout or in the query is inert rather than expanded.
            return (
                SPEECH_PROMPT.replace("{{TASK_ID}}", str(result.get("task_id") or "unknown"))
                .replace("{{ERROR}}", str((execution or {}).get("error") or ""))
                .replace("{{USER_QUERY}}", self.query)
                .replace("{{TASK_OUTPUT}}", output or EMPTY_OUTPUT_PLACEHOLDER)
            )
        except Exception as e:
            raise RuntimeError(f"Error ingesting speech prompt: {e}") from e

    @staticmethod
    def ingest_app_match(requested: str, inventory) -> str:
        """Prompt for matching a spoken app name to something actually installed."""
        try:
            listing = "\n".join(f"- {name}" for name in inventory)
            # Requested last, so an inventory entry cannot be spoofed by a name
            # that happens to contain a token.
            return RESOLVE_APP_PROMPT.replace("{{INVENTORY}}", listing).replace(
                "{{REQUESTED}}", str(requested)
            )
        except Exception as e:
            raise RuntimeError(f"Error ingesting app match prompt: {e}") from e

    @staticmethod
    def ingest_package_id(requested: str) -> str:
        """Prompt for the winget identifier of an application."""
        try:
            return PACKAGE_ID_PROMPT.replace("{{REQUESTED}}", str(requested))
        except Exception as e:
            raise RuntimeError(f"Error ingesting package id prompt: {e}") from e
