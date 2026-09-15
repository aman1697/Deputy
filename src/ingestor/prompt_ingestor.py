from __future__ import annotations

import json

from src.ingestor.context_ingest import Ingestor
from src.prompts.router_prompt import FALLBACK_TASK_ID, ROUTER_PROMPT


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
