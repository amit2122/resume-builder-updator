"""Shared ATS utilities — used by both OllamaATSAgent and HFATSAgent.

Contains only re-usable helpers:
  - _tex_to_text()      : strip LaTeX markup → plain lower-case text
  - _extract_keywords() : flatten any keywords JSON format → list[str]
"""

import json
import re
from pathlib import Path

_TEX_ENV_RE = re.compile(r"\\(?:begin|end)\{[^}]+\}")
_TEX_ANY_COMMAND_RE = re.compile(r"\\[a-zA-Z@]+\*?(?:\[[^\]]*\])?(?:\{[^}]*\})*")
_COMMENT_RE = re.compile(r"(?<!\\)%.*$", re.MULTILINE)  # skip \% (escaped percent, not a comment)
_EXTRA_SPACE_RE = re.compile(r"\s{2,}")


def tex_to_text(tex: str) -> str:
    """Strip LaTeX markup and return plain lower-case text for keyword matching."""
    text = _COMMENT_RE.sub(" ", tex)
    text = _TEX_ENV_RE.sub(" ", text)
    # Unwrap formatting commands — keep the content (e.g. \textbf{40\%} → 40\%)
    text = re.sub(r"\\(?:textbf|textit|emph|text)\{([^}]*)\}", r"\1", text)
    # Unwrap \href{url}{display} → keep display text
    text = re.sub(r"\\href\{[^}]*\}\{([^}]*)\}", r"\1", text)
    # Strip remaining commands with optional args
    text = _TEX_ANY_COMMAND_RE.sub(" ", text)
    text = re.sub(r"[{}&\\]", " ", text)      # leftover special chars
    text = _EXTRA_SPACE_RE.sub(" ", text)
    return text.lower()


def extract_keywords(keywords_json: dict) -> list[str]:
    """Flatten a keywords JSON (standard or companies format) into a flat list of strings."""
    keywords: list[str] = []

    def _add(value):
        if isinstance(value, str) and len(value.strip()) > 2:
            keywords.append(value.strip().lower())
        elif isinstance(value, list):
            for item in value:
                _add(item)
        elif isinstance(value, dict):
            for v in value.values():
                _add(v)

    # Unwrap keyword_summary wrapper (produced by OllamaAnalyserAgent / HFAnalyserAgent)
    root = keywords_json.get("keyword_summary", keywords_json)

    # Process in priority order: most role-specific first so the keyword cap
    # in ATS agents always selects the most relevant terms, pushing noisy
    # secondary/infrastructure keywords beyond the cap.
    for key in (
        "primary_keywords",       # core role requirements — highest priority
        "domain_keywords",        # role-specific concepts (RAG, Agentic AI, etc.)
        "frameworks_libraries",   # specific tools (LangChain, LangGraph, etc.)
        "secondary_keywords",     # broader skills — lower priority
        "tools_and_platforms",    # infra/cloud tools — lowest priority (may include noise)
        "required_skills",
        "preferred_qualifications",
        "key_responsibilities",
    ):
        if key in root:
            _add(root[key])

    # Companies format
    if "companies" in keywords_json:
        for company in keywords_json["companies"]:
            _add(company.get("requiredSkills", []))
            _add(company.get("preferredQualifications", []))
            _add(company.get("keyResponsibilities", []))

    seen: set[str] = set()
    result: list[str] = []
    for kw in keywords:
        parts = re.split(r"[,;.()]+", kw)
        for part in parts:
            clean = re.sub(r"[^a-z0-9 .+#/]", " ", part.strip()).strip()
            clean = _EXTRA_SPACE_RE.sub(" ", clean).strip()
            clean = re.sub(
                r"^(strong |hands.on |experience with |familiarity with |"
                r"knowledge of |proficiency in |ability to )",
                "", clean,
            ).strip()
            if clean and clean not in seen and len(clean) > 2:
                seen.add(clean)
                result.append(clean)
    return result


def load_keywords_json(keywords_path: str) -> dict:
    """Load and unwrap a keywords JSON file, handling raw_response wrapper if present."""
    data = json.loads(Path(keywords_path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "raw_response" in data:
        raw = data["raw_response"]
        try:
            data = json.loads(raw.strip().strip("```json").strip("```").strip())
        except json.JSONDecodeError:
            data = {}
    return data
