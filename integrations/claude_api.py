"""Thin Anthropic Claude wrapper used by every agent that needs LLM output.

Centralizes:
  - Auth (ANTHROPIC_API_KEY env)
  - Prompt caching on the system prompt (system blocks are reused across
    every product/signal in a batch — caching them ~halves cost)
  - JSON-mode parsing with retry on parse failure
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from anthropic import Anthropic


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ClaudeJSONParseError(RuntimeError):
    pass


class ClaudeClient:
    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self.client = Anthropic(api_key=key)

    def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Call Claude, expect JSON, return parsed dict.

        The `system` block is sent with cache_control so repeated calls in the
        same batch (e.g. scoring 30 products one-by-one) reuse the cache.
        """
        resp = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        ).strip()

        # Tolerate fenced code blocks even when we asked for raw JSON.
        m = _JSON_FENCE.search(text)
        candidate = m.group(1).strip() if m else text

        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            raise ClaudeJSONParseError(
                f"failed to parse JSON from model {model}: {e}\n--- raw ---\n{text[:1000]}"
            ) from e
