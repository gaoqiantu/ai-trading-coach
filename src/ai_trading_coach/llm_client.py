from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from ai_trading_coach.config import AppConfig


@dataclass(frozen=True)
class LlmResult:
    content: str
    raw: Any


class LlmClient:
    """
    OpenAI-compatible chat client.

    Guardrails:
    - Only used for journaling/retrospective analysis.
    - Caller should pass ONLY historical facts + evidence.
    - No order placement / predictions.
    """

    def __init__(self, cfg: AppConfig):
        if not cfg.llm_api_key:
            raise RuntimeError(
                "Missing LLM API key. Set LLM_API_KEY (or OPENAI_API_KEY)."
            )
        self._cfg = cfg
        self._client = OpenAI(
            api_key=cfg.llm_api_key,
            base_url=cfg.llm_base_url,
        )

    def chat(self, *, system: str, user: str, temperature: float = 0.2) -> LlmResult:
        resp = self._client.chat.completions.create(
            model=self._cfg.llm_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
        )
        content = resp.choices[0].message.content or ""
        return LlmResult(content=content, raw=resp)


