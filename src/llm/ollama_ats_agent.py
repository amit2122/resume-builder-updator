"""Ollama ATS Scorer Agent — LLM-powered resume scoring using local Ollama model.

Follows the same class pattern as OllamaAnalyserAgent:
  - Loads its own prompt from src/core/prompt_template/ats_scorer_prompt.md
  - Delegates LLM calling to OllamaService (singleton)
  - Shared LaTeX/keyword helpers imported from src.core.ats_utils
"""

import json
from pathlib import Path

from src.llm.ollama_service import Ollama
from src.core.logger import logger
from src.core.ats_utils import tex_to_text, extract_keywords, load_keywords_json

_PROMPT_FILE = (
    Path(__file__).parent.parent / "core" / "prompt_template" / "ats_scorer_prompt.md"
)

AGENT_NAME = "ats_scorer"

# Ollama runs qwen2.5:3b — keep payload well within NUM_CTX=4096
_MAX_RESUME_CHARS = 1500   # plain-text chars sent to LLM
_MAX_KEYWORDS = 25         # keyword list sent to LLM


class OllamaATSAgent:
    """
    LLM-powered ATS scorer backed by local Ollama model.
    Receives the generated resume (.tex) and keywords JSON, asks the LLM
    to evaluate keyword fit and structure, returns a structured score dict.
    """

    def __init__(self, llm_service=None) -> None:
        logger.info("[OLLAMA ATS AGENT] Initializing...")
        self.llm = llm_service or Ollama
        self._system_prompt = _PROMPT_FILE.read_text(encoding="utf-8")
        logger.info("[OLLAMA ATS AGENT] Prompt loaded from: %s", _PROMPT_FILE.name)

    def score(
        self,
        tex_path: str,
        keywords_path: str,
        job_title: str,
        job_descriptions: list[dict] | None = None,
    ) -> dict:
        """
        Score the generated resume against job keywords using the local LLM.

        Args:
            tex_path:         Absolute path to the generated .tex resume.
            keywords_path:    Absolute path to the saved keywords JSON.
            job_title:        Target job role (e.g. "Gen AI Engineer").
            job_descriptions: Optional list of JD dicts for extra context.

        Returns:
            Structured score dict with ats_score, grade, breakdown, and recommendations.
        """
        logger.info(
            "[OLLAMA ATS AGENT] Starting ATS scoring | job=%s | tex=%s",
            job_title, tex_path,
        )

        # --- Build user content ---
        tex = Path(tex_path).read_text(encoding="utf-8")
        resume_plain = tex_to_text(tex)

        kw_data = load_keywords_json(keywords_path)
        keywords = extract_keywords(kw_data)

        # Truncate for small context window
        resume_excerpt = resume_plain[:_MAX_RESUME_CHARS]
        kw_list = keywords[:_MAX_KEYWORDS]

        user_content = self._build_user_content(job_title, kw_list, resume_excerpt)
        payload_size = len(user_content)
        logger.info(
            "[OLLAMA ATS AGENT] Payload: %d keywords (capped at %d), %d resume chars, %d total chars",
            len(kw_list), _MAX_KEYWORDS, len(resume_excerpt), payload_size,
        )

        # --- Call LLM ---
        raw = self.llm.call(
            agent_name=AGENT_NAME,
            system_prompt=self._system_prompt,
            user_content=user_content,
            max_tokens=800,
        )

        # --- Parse response ---
        result = self._parse_response(raw)
        if result is None:
            logger.warning("[OLLAMA ATS AGENT] LLM response could not be parsed.")
            return {"ats_score": None, "grade": "N/A", "error": "LLM response could not be parsed", "scorer": "failed"}

        result["scorer"] = "ollama_llm"

        logger.info(
            "[OLLAMA ATS AGENT] ATS score: %s/100 (%s) via %s",
            result.get("ats_score"), result.get("grade"), result.get("scorer"),
        )
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_user_content(self, job_title: str, keywords: list[str], resume_text: str) -> str:
        kw_block = "\n".join(f"- {kw}" for kw in keywords)
        return (
            f"Job Role: {job_title}\n\n"
            f"Keywords to match:\n{kw_block}\n\n"
            f"Resume Text:\n{resume_text}"
        )

    def _parse_response(self, raw: str) -> dict | None:
        """Strip markdown fences and parse JSON from LLM response."""
        cleaned = raw.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[len("```json"):].strip()
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:].strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

        # Extract the first {...} block in case the model adds stray text
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1:
            logger.warning("[OLLAMA ATS AGENT] No JSON object found in response.")
            return None

        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            logger.warning("[OLLAMA ATS AGENT] JSON parse error: %s", exc)
            return None

        # Minimal validation
        if "ats_score" not in data:
            logger.warning("[OLLAMA ATS AGENT] Response missing 'ats_score' key.")
            return None

        return data
