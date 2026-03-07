"""LiteLLM-based LLM client for summarization (llm mode only)."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from mimir.domain.errors import TreeDexError

logger = logging.getLogger(__name__)


class LlmError(TreeDexError):
    """LLM API call failed."""


class LiteLlmClient:
    """LLM client wrapping litellm for model-agnostic completions.

    Supports Claude, OpenAI, Ollama, and any OpenAI-compatible API.
    """

    def __init__(
        self,
        model: str = "claude-haiku-4-5-20251001",
        max_concurrent: int = 10,
        api_base: Optional[str] = None,
    ) -> None:
        self._model = model
        self._api_base = api_base
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def complete(self, prompt: str) -> str:
        async with self._semaphore:
            try:
                import litellm
                litellm.suppress_debug_info = True  # STFU litellm
                import logging
                logging.getLogger("LiteLLM").setLevel(logging.WARNING)

                kwargs = {
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1024,
                    "temperature": 0.0,
                }
                if self._api_base:
                    kwargs["api_base"] = self._api_base

                response = await litellm.acompletion(**kwargs)
                return response.choices[0].message.content or ""
            except ImportError:
                raise LlmError("litellm not installed. Install with: pip install litellm")
            except Exception as exc:
                raise LlmError(f"LLM completion failed: {exc}") from exc

    async def batch_complete(
        self,
        prompts: list[str],
        *,
        concurrency: int = 10,
    ) -> list[str]:
        """Send multiple prompts with concurrency control."""
        sem = asyncio.Semaphore(concurrency)

        async def _call(prompt: str) -> str:
            async with sem:
                return await self.complete(prompt)

        tasks = [_call(p) for p in prompts]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        output: list[str] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error("LLM call %d failed: %s", i, result)
                output.append("")  # graceful degradation
            else:
                output.append(result)

        return output
