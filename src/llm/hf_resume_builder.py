"""HuggingFace Resume Builder — rewrites a LaTeX resume using HuggingFace LLM."""

import json
import re as _re
import shutil
import subprocess
from pathlib import Path

from src.llm.hf_service import HuggingFaceService
from src.core.logger import logger

_PROMPT_FILE = (
    Path(__file__).parent.parent / "core" / "prompt_template" / "resume_builder_prompt.md"
)

AGENT_NAME = "resume_builder"


def rewrite_resume_with_hf(
    resume_path: str,
    keywords_path: str = "data/Suggested Keyword/gen_ai_engineer_keywords.json",
    output_path: str = "data/Generated Resume/Amit_Resume_Generated.tex",
    llm_service: HuggingFaceService | None = None,
    job_role: str = "",
    job_descriptions: list | None = None,
) -> str:
    """
    Rewrite a LaTeX resume using HuggingFace LLM, guided by extracted recruiter keywords.

    Args:
        resume_path: Path to the original LaTeX resume.
        keywords_path: Path to the extracted keywords JSON.
        output_path: Path to save the generated resume.
        llm_service: HuggingFaceService instance (created fresh if not provided).
        job_role: Target job role string for ATS tailoring.
        job_descriptions: List of JD dicts with 'company' and 'description' keys.

    Returns:
        The generated resume LaTeX text.
    """
    logger.info("[HF RESUME BUILDER] Reading resume from %s", resume_path)
    resume_text = Path(resume_path).read_text(encoding="utf-8")

    # Split original into preamble (never touched by LLM) and body (LLM rewrites this)
    preamble, body = _split_latex(resume_text)
    logger.info("[HF RESUME BUILDER] Preamble extracted (%d chars), body extracted (%d chars)",
                len(preamble), len(body))

    logger.info("[HF RESUME BUILDER] Reading keywords from %s", keywords_path)
    keywords_json = json.loads(Path(keywords_path).read_text(encoding="utf-8"))

    # Unwrap raw_response if the analyser returned non-JSON
    keywords_context: str
    if isinstance(keywords_json, dict) and "raw_response" in keywords_json:
        raw = keywords_json["raw_response"].strip()
        if raw.startswith("```json"):
            raw = raw[len("```json"):].strip()
        elif raw.startswith("```"):
            raw = raw[3:].strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()
        try:
            keywords_json = json.loads(raw)
            keywords_context = json.dumps(_normalize_keywords(keywords_json), indent=2, ensure_ascii=False)
            logger.info("[HF RESUME BUILDER] Successfully parsed raw_response as JSON.")
        except json.JSONDecodeError:
            logger.warning(
                "[HF RESUME BUILDER] Keywords JSON invalid — using raw LLM text as context."
            )
            keywords_context = raw  # use the raw text directly as context
    else:
        keywords_context = json.dumps(_normalize_keywords(keywords_json), indent=2, ensure_ascii=False)

    jd_text = ""
    if job_descriptions:
        jd_text = "\n\n".join(
            f"Company: {jd.get('company', '')}\n{jd.get('description', '')}"
            for jd in job_descriptions
            if jd.get("description")
        )

    logger.info("[HF RESUME BUILDER] Loading prompt and calling HuggingFaceService...")
    system_prompt = _PROMPT_FILE.read_text(encoding="utf-8")

    # Send ONLY the body to the LLM — preamble is injected programmatically
    user_content = (
        f"Target Job Role: {job_role}\n"
        "---\n"
        "Job Descriptions:\n"
        + (jd_text if jd_text else "(not provided)")
        + "\n---\n"
        "Extracted Recruiter Keywords and Trends:\n"
        + keywords_context
        + "\n---\n"
        "Original Resume Body (LaTeX — content between \\begin{document} and \\end{document}):\n"
        + body
        + "\n---\n"
    )
    llm = llm_service or HuggingFaceService()
    rewritten_body = llm.call(
        agent_name=AGENT_NAME,
        system_prompt=system_prompt,
        user_content=user_content,
        max_tokens=4000,
    )

    # Stitch original preamble + LLM-rewritten body back together
    full_resume = _stitch(preamble, rewritten_body, resume_text)

    return _save_and_compile(full_resume, output_path, resume_path)


def _normalize_keywords(kw: dict) -> dict:
    """Normalize keyword JSON to a consistent structure.

    Handles both standard format (primary_keywords, tools_and_platforms, ...)
    and companies format (companies[].requiredSkills, preferredQualifications, ...).
    """
    if "primary_keywords" in kw or "tools_and_platforms" in kw:
        return kw

    if "companies" in kw and isinstance(kw["companies"], list):
        required: list[str] = []
        preferred: list[str] = []
        responsibilities: list[str] = []
        for company in kw["companies"]:
            required.extend(company.get("requiredSkills", []))
            preferred.extend(company.get("preferredQualifications", []))
            responsibilities.extend(company.get("keyResponsibilities", []))
        seen: set[str] = set()
        def dedup(lst: list[str]) -> list[str]:
            result = []
            for item in lst:
                if item not in seen:
                    seen.add(item)
                    result.append(item)
            return result
        return {
            "required_skills": dedup(required),
            "preferred_qualifications": dedup(preferred),
            "key_responsibilities": dedup(responsibilities),
        }

    return kw


def _split_latex(resume_text: str) -> tuple[str, str]:
    """Split a LaTeX document into preamble and body."""
    begin = resume_text.find(r"\begin{document}")
    end = resume_text.rfind(r"\end{document}")
    if begin == -1 or end == -1:
        raise ValueError("Could not find \\begin{document} or \\end{document} in resume.")
    preamble = resume_text[: begin + len(r"\begin{document}")]
    body = resume_text[begin + len(r"\begin{document}") : end].strip()
    return preamble, body


def _stitch(preamble: str, llm_body: str, original_resume: str) -> str:
    """Combine the original preamble with the LLM-rewritten body."""
    body = llm_body.strip()
    if body.startswith("```latex"):
        body = body[len("```latex"):].strip()
    elif body.startswith("```"):
        body = body[3:].strip()
    if body.endswith("```"):
        body = body[:-3].strip()

    if r"\documentclass" in body or r"\begin{document}" in body:
        logger.warning(
            "[HF RESUME BUILDER] LLM returned a full document instead of body only — "
            "falling back to original resume."
        )
        return original_resume

    if len(body) < 200:
        logger.warning(
            "[HF RESUME BUILDER] LLM body output is too short (%d chars) — "
            "falling back to original resume.", len(body)
        )
        return original_resume

    return preamble + "\n\n" + body + "\n\n\\end{document}\n"


def _save_and_compile(full_resume: str, output_path: str, resume_path: str) -> str:
    """Validate, save .tex file, copy resume.cls, and compile to PDF."""
    output_dir = Path(output_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    if not full_resume.lstrip().startswith(r"\documentclass{resume}"):
        logger.error(
            "[HF RESUME BUILDER] Output does not start with \\documentclass{resume}. "
            "Aborting save."
        )
        raise ValueError("Generated LaTeX does not start with \\documentclass{resume}.")

    Path(output_path).write_text(full_resume, encoding="utf-8")
    logger.info("[HF RESUME BUILDER] Generated LaTeX resume saved to %s", output_path)

    cls_source = Path(resume_path).parent / "resume.cls"
    cls_dest = output_dir / "resume.cls"
    if cls_source.exists():
        shutil.copy2(str(cls_source), str(cls_dest))
        logger.info("[HF RESUME BUILDER] Copied resume.cls to %s", cls_dest)
    else:
        logger.warning("[HF RESUME BUILDER] resume.cls not found at %s. PDF may fail.", cls_source)

    tex_file = str(Path(output_path).resolve())
    pdf_output_dir = str(output_dir.resolve())
    compiler = _compile_latex(tex_file, pdf_output_dir, "[HF RESUME BUILDER]")
    if compiler:
        _enforce_one_page(output_path, pdf_output_dir, compiler)

    return full_resume


def _count_pages(pdf_path: str) -> int:
    """Return the number of pages in a PDF using pdfinfo. Returns 0 on error."""
    try:
        result = subprocess.run(
            ["pdfinfo", pdf_path],
            capture_output=True, check=True,
        )
        for line in result.stdout.decode(errors="replace").splitlines():
            if line.lower().startswith("pages:"):
                return int(line.split(":")[1].strip())
    except Exception as e:
        logger.warning("[PAGE CHECK] pdfinfo failed: %s", e)
    return 0


def _enforce_one_page(tex_path: str, output_dir: str, compiler: str) -> None:
    """Iteratively trim the .tex until the compiled PDF is exactly 1 page."""
    pdf_path = str(Path(tex_path).with_suffix(".pdf"))
    pages = _count_pages(pdf_path)
    if pages <= 1:
        logger.info("[PAGE CHECK] Resume is %d page(s). No trimming needed.", pages)
        return

    logger.warning("[PAGE CHECK] Resume is %d pages. Trimming to 1 page...", pages)
    tex = Path(tex_path).read_text(encoding="utf-8")

    # Step 1: collapse blank lines inside itemize
    trimmed = _re.sub(r"(\\item[^\n]*\n)\n+", r"\1", tex)
    if trimmed != tex:
        tex = trimmed
        Path(tex_path).write_text(tex, encoding="utf-8")
        _compile_latex(tex_path, output_dir, "[PAGE-TRIM-1]", compiler_override=compiler)
        if _count_pages(pdf_path) <= 1:
            logger.info("[PAGE CHECK] Fixed after collapsing blank lines.")
            return

    # Step 2: truncate long bullets
    def shorten_item(m):
        line = m.group(0)
        return line[:130].rstrip() + "." if len(line) > 160 else line
    trimmed = _re.sub(r"\\item .+", shorten_item, tex)
    if trimmed != tex:
        tex = trimmed
        Path(tex_path).write_text(tex, encoding="utf-8")
        _compile_latex(tex_path, output_dir, "[PAGE-TRIM-2]", compiler_override=compiler)
        if _count_pages(pdf_path) <= 1:
            logger.info("[PAGE CHECK] Fixed after shortening long bullets.")
            return

    # Step 3: drop last \item in Experience
    exp_match = _re.search(
        r"(\\begin\{rSection\}\{Experience\}.*?)(    \\item [^\n]+\n)(.*?\\end\{rSection\})",
        tex, _re.DOTALL
    )
    if exp_match:
        items = list(_re.finditer(r"    \\item .+\n", exp_match.group(0)))
        if items:
            last = items[-1]
            s = exp_match.start() + last.start()
            e = exp_match.start() + last.end()
            tex = tex[:s] + tex[e:]
            Path(tex_path).write_text(tex, encoding="utf-8")
            _compile_latex(tex_path, output_dir, "[PAGE-TRIM-3]", compiler_override=compiler)
            if _count_pages(pdf_path) <= 1:
                logger.info("[PAGE CHECK] Fixed after removing last experience bullet.")
                return

    # Step 4: remove Patents section entirely
    trimmed = _re.sub(
        r"\\begin\{rSection\}\{Patents\}.*?\\end\{rSection\}",
        "", tex, flags=_re.DOTALL
    )
    if trimmed != tex:
        tex = trimmed
        Path(tex_path).write_text(tex, encoding="utf-8")
        _compile_latex(tex_path, output_dir, "[PAGE-TRIM-4]", compiler_override=compiler)
        if _count_pages(pdf_path) <= 1:
            logger.info("[PAGE CHECK] Fixed after removing Patents section.")
            return

    logger.warning("[PAGE CHECK] Could not trim to 1 page after all strategies.")


def _compile_latex(tex_file: str, output_dir: str, log_prefix: str, compiler_override: str | None = None) -> str | None:
    """Try xelatex first (Overleaf-compatible), fall back to pdflatex.

    Returns the name of the compiler that succeeded, or None if all failed.
    """
    compilers = [compiler_override] if compiler_override else ["xelatex", "pdflatex"]
    for compiler in compilers:
        try:
            logger.info("%s Compiling with %s: %s", log_prefix, compiler, tex_file)
            result = subprocess.run(
                [compiler, "-interaction=nonstopmode", f"-output-directory={output_dir}", tex_file],
                check=True,
                capture_output=True,
                cwd=output_dir,
            )
            logger.info("%s PDF generated successfully with %s in %s", log_prefix, compiler, output_dir)
            if result.stdout:
                logger.debug(
                    "%s %s stdout: %s",
                    log_prefix, compiler,
                    result.stdout.decode(errors="replace")[-500:],
                )
            return compiler
        except subprocess.CalledProcessError as e:
            logger.warning(
                "%s %s failed (exit %s) — %s",
                log_prefix, compiler, e.returncode,
                e.stderr.decode(errors="replace")[-500:],
            )
        except FileNotFoundError:
            logger.warning("%s %s not found, trying next compiler.", log_prefix, compiler)

    logger.error(
        "%s All LaTeX compilers failed. Install texlive: sudo apt-get install texlive-xetex texlive-latex-base",
        log_prefix,
    )
    return None
