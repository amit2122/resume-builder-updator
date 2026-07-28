"""Ollama Analyser Agent — extracts recruiter keywords using local Ollama LLM."""

import json
from pathlib import Path
from typing import Any

from src.llm.ollama_service import Ollama
from src.core.logger import logger

_PROMPT_FILE = (
    Path(__file__).parent.parent / "core" / "prompt_template" / "analyser_agent_prompt.md"
)

AGENT_NAME = "analyser"
_MAX_CHARS_PER_JD = 800   # Only key_responsibilities, keep well within context window
_MAX_JDS = 3              # Send at most 3 JDs to avoid exceeding context window


class OllamaAnalyserAgent:
    """
    Keyword extraction agent backed by local Ollama LLM.
    Owns its system prompt and user content formatting.
    Delegates LLM calling to OllamaService.
    """

    def __init__(self, llm_service=None) -> None:
        logger.info("[OLLAMA ANALYSER AGENT] Initializing...")
        self.llm = llm_service or Ollama
        self._system_prompt = _PROMPT_FILE.read_text(encoding="utf-8")
        logger.info("[OLLAMA ANALYSER AGENT] Prompt loaded from: %s", _PROMPT_FILE.name)

    def analyze(self, job_input: dict[str, Any]) -> str:
        """
        Analyze job descriptions and extract recruiter keywords.

        Args:
            job_input: Dict with keys 'job_role' and 'job_descriptions'.

        Returns:
            LLM response as a clean JSON string (markdown fences stripped).
        """
        logger.info(
            "[OLLAMA ANALYSER AGENT] Starting analysis | job_role=%s | jd_count=%s",
            job_input.get("job_role", "unknown"),
            len(job_input.get("job_descriptions", [])),
        )

        # Deduplicate by company name and cap at _MAX_JDS to stay within context window.
        # Prefer key_responsibilities over full description — much shorter and more signal-dense.
        seen_companies: set[str] = set()
        compact_jds: list[dict] = []
        for jd in job_input.get("job_descriptions", []):
            company = jd.get("company", "unknown")
            if company in seen_companies:
                continue
            seen_companies.add(company)
            # Use key_responsibilities if present (scraped field), else fall back to description
            text = (
                jd.get("key_responsibilities")
                or jd.get("description", "")
            )
            compact_jds.append({
                "company": company,
                "location": jd.get("location", ""),
                "description": text[:_MAX_CHARS_PER_JD],
            })
            if len(compact_jds) >= _MAX_JDS:
                break

        truncated_input = {
            "job_role": job_input.get("job_role", ""),
            "job_descriptions": compact_jds,
        }
        payload_size = len(json.dumps(truncated_input, ensure_ascii=False))
        logger.info(
            "[OLLAMA ANALYSER AGENT] Payload: %d JDs, %d chars (limit: %d chars/JD, max %d JDs)",
            len(compact_jds), payload_size, _MAX_CHARS_PER_JD, _MAX_JDS,
        )
        user_content = json.dumps(truncated_input, ensure_ascii=False)
        result = self.llm.call(
            agent_name=AGENT_NAME,
            system_prompt=self._system_prompt,
            user_content=user_content,
            max_tokens=4000,
        )
        result = self._clean_json_response(result)
        logger.info("[OLLAMA ANALYSER AGENT] Analysis complete.")
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
