"""HuggingFace Analyser Agent — extracts recruiter keywords using HuggingFace LLM."""

import json
from pathlib import Path
from typing import Any

from src.llm.hf_service import HuggingFaceService
from src.core.logger import logger

_PROMPT_FILE = (
    Path(__file__).parent.parent / "core" / "prompt_template" / "analyser_agent_prompt.md"
)

AGENT_NAME = "analyser"
_MAX_CHARS_PER_JD = 3000  # Truncate each JD description to avoid 504 timeouts


class HFAnalyserAgent:
    """
    Keyword extraction agent backed by HuggingFace Inference API.
    Owns its system prompt and user content formatting.
    Delegates LLM calling to HuggingFaceService.
    """

    def __init__(self, llm_service: HuggingFaceService | None = None) -> None:
        logger.info("[HF ANALYSER AGENT] Initializing...")
        self.llm = llm_service or HuggingFaceService()
        self._system_prompt = _PROMPT_FILE.read_text(encoding="utf-8")
        logger.info("[HF ANALYSER AGENT] Prompt loaded from: %s", _PROMPT_FILE.name)

    def analyze(self, job_input: dict[str, Any]) -> str:
        """
        Analyze job descriptions and extract recruiter keywords.

        Args:
            job_input: Dict with keys 'job_role' and 'job_descriptions'.

        Returns:
            LLM response as a clean JSON string (markdown fences stripped).
        """
        logger.info(
            "[HF ANALYSER AGENT] Starting analysis | job_role=%s | jd_count=%s",
            job_input.get("job_role", "unknown"),
            len(job_input.get("job_descriptions", [])),
        )
        # Truncate each JD description to reduce payload size and avoid gateway timeouts
        truncated_input = {
            "job_role": job_input.get("job_role", ""),
            "job_descriptions": [
                {
                    **jd,
                    "description": jd.get("description", "")[:_MAX_CHARS_PER_JD],
                }
                for jd in job_input.get("job_descriptions", [])
            ],
        }
        payload_size = len(json.dumps(truncated_input, ensure_ascii=False))
        logger.info("[HF ANALYSER AGENT] Payload size after truncation: %s chars", payload_size)
        user_content = json.dumps(truncated_input, ensure_ascii=False)
        result = self.llm.call(
            agent_name=AGENT_NAME,
            system_prompt=self._system_prompt,
            user_content=user_content,
            max_tokens=4000,
        )
        result = self._clean_json_response(result)
        logger.info("[HF ANALYSER AGENT] Analysis complete.")
        return result

    def _clean_json_response(self, text: str) -> str:
        """Strip markdown code fences from LLM JSON response."""
        cleaned = text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[len("```json"):].strip()
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:].strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
        return cleaned
