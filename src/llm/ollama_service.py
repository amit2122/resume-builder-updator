"""Ollama service module providing a LiteLLM-backed model interface."""

import json
from smolagents import LiteLLMModel
from src.core.config import settings
from src.core.logger import logger

# How long (seconds) to wait for a single Ollama response.
# qwen2.5:3b on a low-RAM machine needs up to ~120s for a ~1500-char payload.
_REQUEST_TIMEOUT = 180

class OllamaService:
    """Service wrapper around a locally running Ollama model via LiteLLM."""

    def __init__(self):
        """Initialize the service and load the configured LiteLLM model."""
        logger.info("Initializing OllamaLocal service...")
        self.model = self.get_model()
        logger.info("OllamaLocal service initialized successfully")

    def get_response(self, query: str, context: dict | None = None) -> str:
        """
        Send a query to the model and return its response as a string.

        Args:
            query: The user's question or prompt.
            context: Optional dictionary of contextual data prepended to the query.

        Returns:
            The model's response as a plain string.
        """
        logger.info("Getting response from OllamaLocal service...")
        if context:
            context_text = json.dumps(context, ensure_ascii=True)
            user_text = f"Context: {context_text}\nUser Query: {query}"
        else:
            user_text = query

        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": user_text}],
            }
        ]
        response = self.model(messages)
        content = getattr(response, "content", None)
        if isinstance(content, list):
            response_text = json.dumps(content, ensure_ascii=True)
        elif isinstance(content, str):
            response_text = content
        else:
            response_text = str(response)
        logger.info("Response from OllamaLocal service: %s", response_text)
        return response_text

    def call(self, agent_name: str, system_prompt: str, user_content: str, max_tokens: int = 2000) -> str:
        """
        Call the Ollama model with a system prompt and user content.
        Matches the HuggingFaceService.call() interface for drop-in compatibility.
        """
        logger.info(
            "[OLLAMA SERVICE] Request | agent=%s | model=%s | max_tokens=%s | payload=%s chars",
            agent_name, settings.MODEL_ID, max_tokens, len(user_content),
        )
        messages = [
            {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "text", "text": user_content}]},
        ]
        response = self.model(messages)
        content = getattr(response, "content", None)
        if isinstance(content, list):
            result = " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        elif isinstance(content, str):
            result = content
        else:
            result = str(response)
        logger.info(
            "[OLLAMA SERVICE] Response received | agent=%s | length=%s chars",
            agent_name, len(result),
        )
        return result

    def get_model(self):
        """Instantiate and return the LiteLLMModel using settings from config."""
        return LiteLLMModel(
            model_id=settings.MODEL_ID,
            api_base=settings.API_BASE,
            num_ctx=settings.NUM_CTX,
            timeout=_REQUEST_TIMEOUT,
        )

Ollama = OllamaService()
