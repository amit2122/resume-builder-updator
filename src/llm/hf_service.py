"""HuggingFace Hub LLM service — pure calling layer, no agent logic."""

import time
from typing import Any
from huggingface_hub import InferenceClient
from requests.exceptions import HTTPError
from src.core.config import settings
from src.core.logger import logger

_MAX_RETRIES = 3
_RETRY_DELAY = 5  # seconds between retries


class HuggingFaceService:
    """
    Pure LLM service wrapper around HuggingFace InferenceClient.
    Agents are responsible for loading their own prompts and formatting user content.
    """

    def __init__(self) -> None:
        if not settings.HF_MODEL or not settings.HF_TOKEN:
            raise RuntimeError(
                "HF_MODEL and HF_TOKEN must be set in .env to use the HuggingFace backend. "
                "Use /scrape/ollama instead if HF credentials are unavailable."
            )
        logger.info("[HF SERVICE] Initializing HuggingFaceService | model=%s", settings.HF_MODEL)
        self.client = InferenceClient(model=settings.HF_MODEL, token=settings.HF_TOKEN)
        logger.info("[HF SERVICE] HuggingFaceService initialized successfully")

    def call(
        self,
        agent_name: str,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 2000,
    ) -> str:
        """
        Call the LLM with the given system prompt and user content.

        Args:
            agent_name: Name of the calling agent (for logging only).
            system_prompt: The system prompt loaded by the agent.
            user_content: The user message formatted by the agent.
            max_tokens: Max tokens for the response.

        Returns:
            The LLM response as a plain string.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        logger.info(
            "[HF SERVICE] Request | agent=%s | model=%s | max_tokens=%s | payload=%s chars",
            agent_name, settings.HF_MODEL, max_tokens, len(user_content),
        )
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = self.client.chat.completions.create(
                    messages=messages,
                    stream=False,
                    max_tokens=max_tokens,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                result = response.choices[0].message.content or ""
                logger.info(
                    "[HF SERVICE] Response received | agent=%s | length=%s chars",
                    agent_name, len(result),
                )
                return result
            except HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if status in (504, 502, 503) and attempt < _MAX_RETRIES:
                    logger.warning(
                        "[HF SERVICE] %s error (attempt %s/%s) | retrying in %ss...",
                        status, attempt, _MAX_RETRIES, _RETRY_DELAY,
                    )
                    time.sleep(_RETRY_DELAY * attempt)
                else:
                    logger.error("[HF SERVICE] Request failed after %s attempts: %s", attempt, e)
                    raise
        return ""
