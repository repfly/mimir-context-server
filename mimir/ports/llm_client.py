"""LlmClient port — interface for LLM completions (used in llm mode only)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LlmClient(Protocol):
    """Interface for LLM text completions.

    Implementation: ``LiteLlmClient``.
    Only instantiated when ``summary_mode == "llm"``.
    """

    async def complete(self, prompt: str) -> str:
        """Send a single completion request.

        Returns
        -------
        str
            The model's text response.
        """
        ...

    async def batch_complete(
        self,
        prompts: list[str],
        *,
        concurrency: int = 10,
    ) -> list[str]:
        """Send multiple completion requests with concurrency control.

        Parameters
        ----------
        prompts
            List of prompt strings.
        concurrency
            Maximum number of concurrent API calls.

        Returns
        -------
        list[str]
            One response per prompt, in corresponding order.
        """
        ...
