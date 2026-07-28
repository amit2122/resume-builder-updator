"""Resume AI Pipeline — FastAPI application.

Endpoints:
  POST /scrape          — Scrape LinkedIn → HuggingFace pipeline (requires HF credits)
  POST /scrape/ollama   — Scrape LinkedIn → Ollama pipeline (fully local, no credits needed)
"""

from typing import Any
from pathlib import Path
import asyncio
import json
import logging
import re
import time
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

# --- Ollama (local) ---
from src.llm.ollama_analyser_agent import OllamaAnalyserAgent
from src.llm.ollama_resume_builder import rewrite_resume_with_ollama

# --- HuggingFace (cloud) ---
from src.llm.hf_analyser_agent import HFAnalyserAgent
from src.llm.hf_resume_builder import rewrite_resume_with_hf

# --- LinkedIn Scraper ---
from src.scraper.linkedin_scraper import scrape_all_jobs

# --- ATS Scorer agents ---
from src.llm.ollama_ats_agent import OllamaATSAgent
from src.llm.hf_ats_agent import HFATSAgent

logger = logging.getLogger(__name__)

app = FastAPI(title="Resume AI Pipeline")

# Ollama agents are fully local (no external credentials) — safe to build at startup.
ollama_analyser = OllamaAnalyserAgent()
ollama_ats = OllamaATSAgent()

# HF agents require HF_TOKEN/HF_MODEL — built lazily on first use so the app
# (and /scrape/ollama in particular) still starts when HF credentials are
# missing or exhausted.
_hf_analyser: HFAnalyserAgent | None = None
_hf_ats: HFATSAgent | None = None


def _get_hf_analyser() -> HFAnalyserAgent:
    global _hf_analyser
    if _hf_analyser is None:
        _hf_analyser = HFAnalyserAgent()
    return _hf_analyser


def _get_hf_ats() -> HFATSAgent:
    global _hf_ats
    if _hf_ats is None:
        _hf_ats = HFATSAgent()
    return _hf_ats


SUGGESTED_KEYWORD_DIR = Path("data/Suggested Keyword")
SUGGESTED_KEYWORD_DIR.mkdir(parents=True, exist_ok=True)
RESUME_PATH = Path("data/Resume/Amit_Resume.tex")
GENERATED_RESUME_PATH = Path("data/Generated Resume/Amit_Resume_Generated.tex")


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

class ScrapeRequest(BaseModel):
    """Controls LinkedIn search parameters for the scrape endpoints."""
    job_titles: list[str] = ["Gen AI Engineer"]
    keywords: list[str] = ["Gen AI Engineer"]
    locations: list[str] = ["India"]
    geo_ids: list[str] = ["102713980"]
    distance: int = 25
    sort_by: str = "DD"
    posted_within: str = "r86400"
    max_jobs: int = 10
    page_size: int = 25
    max_pages: int = 5
    headless: bool = True


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _scrape_to_job_input(request: ScrapeRequest) -> tuple[list, dict[str, Any]]:
    """Run LinkedIn scraper and return (raw_jobs, job_input) ready for the pipeline."""
    config = {
        "job_titles": request.job_titles,
        "keywords_list": request.keywords,
        "locations": request.locations,
        "geo_ids": request.geo_ids,
        "distance": request.distance,
        "sort_by": request.sort_by,
        "posted_within_seconds": request.posted_within,
        "max_jobs": request.max_jobs,
        "page_size": request.page_size,
        "max_pages": request.max_pages,
        "headless": request.headless,
    }
    raw_jobs = scrape_all_jobs(config)
    job_role = (
        request.job_titles[0] if request.job_titles
        else request.keywords[0] if request.keywords
        else "Unknown Role"
    )
    job_input: dict[str, Any] = {
        "job_role": job_role,
        "job_descriptions": [
            {
                "company": j.get("company", ""),
                "location": j.get("location", ""),
                "description": j.get("description", ""),
            }
            for j in raw_jobs
            if j.get("description")
        ],
    }
    return raw_jobs, job_input


def _run_pipeline(job_input: dict[str, Any], analyser, resume_builder_fn, ats_agent, backend: str) -> dict:
    """Keyword extraction + resume rewrite. Returns the response dict."""

    # Step 1 — keyword extraction
    logger.info("[%s] Calling analyser for keyword extraction...", backend)
    start_kw = time.time()
    result_text = analyser.analyze(job_input)
    logger.info("[%s] Keyword extraction done in %.2fs.", backend, time.time() - start_kw)

    try:
        result_data: Any = json.loads(result_text)
        logger.info("[%s] Keyword response parsed as JSON.", backend)
    except json.JSONDecodeError:
        result_data = {"raw_response": result_text}
        logger.warning("[%s] Keyword response was not valid JSON — saving raw.", backend)

    job_role_slug = re.sub(r"[^a-z0-9_-]+", "_", job_input.get("job_role", "output").lower()).strip("_") or "output"
    output_file = SUGGESTED_KEYWORD_DIR / f"{job_role_slug}_keywords.json"
    output_file.write_text(json.dumps(result_data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("[%s] Keywords saved to %s", backend, output_file)

    # Step 2 — resume rewrite
    if not RESUME_PATH.exists():
        logger.error("[%s] Resume not found at %s. Skipping rewrite.", backend, RESUME_PATH)
        return {
            "result": result_data,
            "saved_to": str(output_file),
            "resume_error": f"Resume file not found at {RESUME_PATH}",
        }

    logger.info("[%s] Rewriting resume...", backend)
    try:
        start_rb = time.time()
        resume_builder_fn(
            resume_path=str(RESUME_PATH),
            keywords_path=str(output_file),
            output_path=str(GENERATED_RESUME_PATH),
            job_role=job_input.get("job_role", ""),
            job_descriptions=job_input.get("job_descriptions", []),
        )
        logger.info("[%s] Resume rewrite done in %.2fs. Saved to %s",
                    backend, time.time() - start_rb, GENERATED_RESUME_PATH)
    except (OSError, RuntimeError, ValueError) as e:
        logger.error("[%s] Resume rewrite failed: %s", backend, e)
        return {
            "result": result_data,
            "saved_to": str(output_file),
            "resume_error": f"Resume rewriting failed: {e}",
        }

    # Step 3 — ATS scoring (LLM agent, falls back to pure-Python on error)
    ats: dict = {}
    try:
        ats = ats_agent.score(
            tex_path=str(GENERATED_RESUME_PATH),
            keywords_path=str(output_file),
            job_title=job_input.get("job_role", ""),
            job_descriptions=job_input.get("job_descriptions", []),
        )
        logger.info("[%s] ATS score: %s/100 (%s) via %s", backend, ats.get("ats_score"), ats.get("grade"), ats.get("scorer"))
    except Exception as e:
        logger.warning("[%s] ATS scoring failed (non-fatal): %s", backend, e)

    return {
        "result": result_data,
        "keywords_saved_to": str(output_file),
        "resume_generated": str(GENERATED_RESUME_PATH),
        "ats_score": ats,
        "backend": backend,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/scrape/huggingface", summary="Scrape LinkedIn → HuggingFace pipeline")
async def scrape_and_analyze_hf(request: ScrapeRequest):
    """Full end-to-end pipeline using HuggingFace cloud LLM.

    1. Scrapes LinkedIn jobs matching your filters
    2. Extracts ATS keywords via HuggingFace (moonshotai/Kimi-K2.5)
    3. Rewrites and compiles your resume to PDF

    Requires LINKED_IN_USERNAME + LINKED_IN_PASSWORD in .env.
    Requires valid HuggingFace credits — use /scrape/ollama if credits are exhausted.
    """
    logger.info("[/scrape] Starting HuggingFace pipeline | titles=%s | max_jobs=%s",
                request.job_titles, request.max_jobs)
    try:
        raw_jobs, job_input = await asyncio.to_thread(_scrape_to_job_input, request)
    except ValueError as e:
        logger.error("[/scrape] Credentials/config error: %s", e)
        return JSONResponse(content={"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error("[/scrape] Scraping failed: %s", e)
        return JSONResponse(content={"error": f"LinkedIn scraping failed: {e}"}, status_code=500)

    if not job_input["job_descriptions"]:
        logger.warning("[/scrape] No jobs with descriptions scraped.")
        return JSONResponse(
            content={"error": "No jobs with descriptions were scraped. Check credentials and filters."},
            status_code=404,
        )

    logger.info("[/scrape] Scraped %s jobs. Running HuggingFace pipeline...",
                len(job_input["job_descriptions"]))
    try:
        response_data = _run_pipeline(
            job_input, _get_hf_analyser(), rewrite_resume_with_hf, _get_hf_ats(), "HuggingFace"
        )
        return JSONResponse(content={**response_data, "scraped_jobs": len(raw_jobs)})
    except (OSError, RuntimeError) as e:
        logger.error("[/scrape] Pipeline error: %s", e)
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/scrape/ollama", summary="Scrape LinkedIn → Ollama pipeline (fully local)")
async def scrape_and_analyze_ollama(request: ScrapeRequest):
    """Full end-to-end pipeline using local Ollama LLM — no API credits required.

    1. Scrapes LinkedIn jobs matching your filters
    2. Extracts ATS keywords via local Ollama
    3. Rewrites and compiles your resume to PDF

    Requires LINKED_IN_USERNAME + LINKED_IN_PASSWORD in .env.
    Use this when HuggingFace credits are exhausted.
    """
    logger.info("[/scrape/ollama] Starting Ollama pipeline | titles=%s | max_jobs=%s",
                request.job_titles, request.max_jobs)
    try:
        raw_jobs, job_input = await asyncio.to_thread(_scrape_to_job_input, request)
    except ValueError as e:
        logger.error("[/scrape/ollama] Credentials/config error: %s", e)
        return JSONResponse(content={"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error("[/scrape/ollama] Scraping failed: %s", e)
        return JSONResponse(content={"error": f"LinkedIn scraping failed: {e}"}, status_code=500)

    if not job_input["job_descriptions"]:
        logger.warning("[/scrape/ollama] No jobs with descriptions scraped.")
        return JSONResponse(
            content={"error": "No jobs with descriptions were scraped. Check credentials and filters."},
            status_code=404,
        )

    logger.info("[/scrape/ollama] Scraped %s jobs. Running Ollama pipeline...",
                len(job_input["job_descriptions"]))
    try:
        response_data = _run_pipeline(job_input, ollama_analyser, rewrite_resume_with_ollama, ollama_ats, "Ollama")
        return JSONResponse(content={**response_data, "scraped_jobs": len(raw_jobs)})
    except (OSError, RuntimeError) as e:
        logger.error("[/scrape/ollama] Pipeline error: %s", e)
        return JSONResponse(content={"error": str(e)}, status_code=500)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=19001)
