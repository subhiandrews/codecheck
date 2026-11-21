"""A DeepEval judge backed by the Anthropic API.

DeepEval metrics (GEval, DAGMetric) call a "judge" model. This wraps the Anthropic SDK in
DeepEvalBaseLLM so the judge is Claude rather than DeepEval's default OpenAI model.

Config comes from the environment so nothing secret or model-specific is committed:
  ANTHROPIC_API_KEY       required
  DEEPEVAL_JUDGE_MODEL    judge model id (default below)
"""

from __future__ import annotations

import json
import os
import re

import _env  # noqa: F401  (telemetry opt-out, must run before deepeval is imported)
from anthropic import Anthropic, AsyncAnthropic
from deepeval.models import DeepEvalBaseLLM

DEFAULT_JUDGE_MODEL = "claude-opus-5-5"
MAX_TOKENS = 2048


def _extract_json(text: str) -> dict:
    """Return the first JSON object in a model reply, tolerating a code fence or prose around it."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
    return json.loads(candidate)


class AnthropicJudge(DeepEvalBaseLLM):
    def __init__(self, model: str | None = None) -> None:
        self.model_id = model or os.environ.get("DEEPEVAL_JUDGE_MODEL", DEFAULT_JUDGE_MODEL)
        super().__init__(self.model_id)

    def load_model(self) -> Anthropic:
        return Anthropic()

    def get_model_name(self) -> str:
        return self.model_id

    def _prompt(self, prompt: str, schema) -> str:
        if schema is None:
            return prompt
        shape = json.dumps(schema.model_json_schema())
        return f"{prompt}\n\nReply with only a JSON object matching this JSON schema:\n{shape}"

    @staticmethod
    def _parse(reply, schema):
        # The reply can start with a thinking block, so take the text blocks, not content[0].
        text = "".join(block.text for block in reply.content if block.type == "text")
        if not text:
            raise ValueError(f"judge reply had no text block (stop_reason={reply.stop_reason})")
        return text if schema is None else schema.model_validate(_extract_json(text))

    def generate(self, prompt: str, schema=None):
        reply = self.load_model().messages.create(
            model=self.model_id,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": self._prompt(prompt, schema)}],
        )
        return self._parse(reply, schema)

    async def a_generate(self, prompt: str, schema=None):
        reply = await AsyncAnthropic().messages.create(
            model=self.model_id,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": self._prompt(prompt, schema)}],
        )
        return self._parse(reply, schema)
