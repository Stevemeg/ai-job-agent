"""
resume_tailor.py -- LLM-powered resume tailoring (provider-agnostic).

Takes a job from your ranked list and, via backend.llm (Groq in deployment,
Ollama locally -- auto-selected):
  1. Extracts the JD's key requirements and preferred skills
  2. Rewrites your project bullet points to mirror the JD's language
  3. Suggests a tailored skills ordering (most-relevant first)
Every rewritten bullet passes the deterministic hallucination validator
before reaching generate_resume_docx.js, which produces the ATS-safe .docx.

Run from project root:
    python -m backend.resume_engine.resume_tailor --rank 1
    python -m backend.resume_engine.resume_tailor --rank 3 --model mistral
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import os
from pathlib import Path
from ..config import DATA_DIR, PROFILE_FILE, RANKED_FILE
from ..llm import generate as llm_generate, LLMUnavailableError
from .validate_tailored import validate_project, print_report

# None = "the provider's default": Ollama serves mistral locally; Groq
# serves llama-3.3-70b-versatile in deployment (see backend/llm/).
# Model IDs are provider-specific, so nothing here should assume one.
DEFAULT_MODEL = None
DOCX_SCRIPT = Path(__file__).resolve().parent / "generate_resume_docx.js"

# Backward-compat alias: UI and cover_letter import this name. The provider
# layer raises LLMUnavailableError with a provider-appropriate message
# (start Ollama / set GROQ_API_KEY).
OllamaUnavailableError = LLMUnavailableError


def call_ollama(prompt, model=DEFAULT_MODEL):
    """Legacy name, provider-agnostic body: delegates to backend.llm,
    which routes to Groq (GROQ_API_KEY present) or Ollama (local dev).
    Returns parsed JSON dict or None; raises LLMUnavailableError."""
    return llm_generate(prompt, model=model)


def extract_jd_requirements(jd_text, model):
    """Ask Mistral to extract structured requirements from the JD."""
    prompt = f"""You are an expert resume coach analyzing a job description.
Extract the key information and return ONLY valid JSON with no other text.

Job Description:
{jd_text[:3000]}

Return this exact JSON structure:
{{
  "role_summary": "one sentence describing what this role does",
  "must_have_skills": ["skill1", "skill2"],
  "nice_to_have_skills": ["skill1", "skill2"],
  "key_responsibilities": ["responsibility1", "responsibility2", "responsibility3"],
  "keywords_to_include": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"]
}}"""
    return call_ollama(prompt, model)


def tailor_project_bullets(project, jd_requirements, model):
    """Rewrite a project's bullet points to better mirror the JD's language."""
    if not jd_requirements:
        return project["description"]

    keywords = jd_requirements.get("keywords_to_include", [])
    responsibilities = jd_requirements.get("key_responsibilities", [])

    prompt = f"""You are an expert resume coach. Rewrite the project bullet points below
to better align with this job's requirements, while remaining 100% truthful.

STRICT RULES -- violations make the resume fraudulent:
1. Do NOT add any tool, platform, technology, or company name not in the original bullets.
   BAD: original says "PyTorch" -> you add "on Databricks" -> FRAUDULENT
   BAD: original says "Ollama" -> you add "using LangChain" -> FRAUDULENT
   GOOD: rephrase existing tools using JD's preferred terminology
2. Do NOT invent achievements, metrics, or outcomes not in the original.
3. ONLY mirror JD language where the ORIGINAL already demonstrates that capability.
4. If a JD keyword has NO evidence in the original bullets, DO NOT include it.

What you CAN do:
- Reorder information within a bullet for better impact
- Replace weak verbs with stronger action verbs
- Rephrase descriptions to echo JD terminology (e.g. "built" -> "deployed production-grade")
- Emphasize metrics that already exist in the original

Original project bullets:
{project["description"]}

JD keywords to mirror ONLY where genuinely supported by the original:
{json.dumps(keywords)}

JD key responsibilities:
{json.dumps(responsibilities[:3])}

Return this exact JSON structure:
{{
  "tailored_bullets": [
    "bullet 1 starting with action verb",
    "bullet 2 starting with action verb",
    "bullet 3 starting with action verb"
  ]
}}"""
    result = call_ollama(prompt, model)
    if result and "tailored_bullets" in result:
        return result["tailored_bullets"]
    # Fallback: return original bullets split by •
    return [b.strip() for b in re.split(r'[•\u2022]', project["description"]) if b.strip()]


def order_skills_for_role(skills, jd_requirements, model):
    """Ask Mistral to reorder skills with most-relevant first."""
    if not jd_requirements:
        return [s["skill"] for s in skills]

    must_have = jd_requirements.get("must_have_skills", [])
    nice_to_have = jd_requirements.get("nice_to_have_skills", [])
    skill_names = [s["skill"] for s in skills]

    prompt = f"""You are an expert resume coach. Reorder these skills to put the most
relevant ones first for this specific role. Return ONLY valid JSON.

Candidate skills: {json.dumps(skill_names)}
JD must-have skills: {json.dumps(must_have)}
JD nice-to-have skills: {json.dumps(nice_to_have)}

Return this exact JSON structure (include ALL skills, just reordered):
{{
  "ordered_skills": ["skill1", "skill2", "skill3"]
}}"""
    result = call_ollama(prompt, model)
    if result and "ordered_skills" in result:
        # Validate all original skills are present; fall back if not
        ordered = result["ordered_skills"]
        if set(ordered) == set(skill_names):
            return ordered
    return skill_names  # fallback: original order


def build_tailored_resume(profile, job, jd_requirements, model):
    """Assemble the tailored resume content dict.

    Returns (content_dict, validation_report) -- the report lists, per
    project, whether the LLM output passed the truthfulness gate and which
    terms were flagged, so UIs can show WHY a project fell back."""
    print("  Tailoring project bullets...")
    skill_names = [s["skill"] for s in profile.get("skills", [])]
    tailored_projects = []
    validation_report = []
    for proj in profile["projects"]:
        bullets = tailor_project_bullets(proj, jd_requirements, model)
        if not isinstance(bullets, list):
            bullets = [b.strip() for b in re.split("•", bullets) if b.strip()]

        # Truthfulness gate: verify no tool/metric was hallucinated. If any
        # bullet fails, fall back to the ORIGINAL bullets for that project --
        # an untailored true resume beats a tailored fraudulent one.
        result = validate_project(bullets, proj["description"], skill_names)
        print_report(proj["title"], bullets, result)
        flagged = sorted({f for b in result["bullets"]
                          for f in b["flagged_terms"] + b["flagged_numbers"]})
        validation_report.append({"project": proj["title"],
                                  "ok": result["ok"], "flagged": flagged})
        if not result["ok"]:
            print(f"    -> Falling back to original bullets for '{proj['title']}'")
            bullets = [b.strip() for b in re.split("•", proj["description"])
                       if b.strip()]

        tailored_projects.append({
            "title": proj["title"],
            "duration": proj.get("duration", ""),
            "bullets": bullets,
        })

    print("  Ordering skills by relevance...")
    ordered_skills = order_skills_for_role(profile["skills"], jd_requirements, model)

    content = {
        "candidate": {
            "name": profile.get("name", ""),
            "email": profile.get("email", ""),
            "phone": profile.get("phone", ""),
            "linkedin": profile.get("linkedin", ""),
            "github": profile.get("github", ""),
        },
        "education": profile.get("education", []),
        "skills": ordered_skills,
        "projects": tailored_projects,
        "target_role": job.get("title", ""),
        "target_company": job.get("company", ""),
        "jd_summary": jd_requirements.get("role_summary", "") if jd_requirements else "",
    }
    return content, validation_report


def _write_docx(content, output_path):
    """Hand the content dict to the Node docx generator. Raises RuntimeError
    on failure (node missing, generator error) instead of exiting."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json',
                                     delete=False, encoding='utf-8') as tmp:
        json.dump(content, tmp, ensure_ascii=False, indent=2)
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            ["node", str(DOCX_SCRIPT), tmp_path, str(output_path)],
            capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("Node.js not found -- .docx generation needs "
                           "node on PATH (see docs/DEVELOPER_GUIDE.md).")
    finally:
        os.unlink(tmp_path)
    if result.returncode != 0:
        raise RuntimeError(f"docx generation failed: {result.stderr[:400]}")


def tailor_to_job(profile, job, jd_text, model=DEFAULT_MODEL):
    """Full tailoring pipeline for one job -- the entry point for the UI
    and future API. Returns (docx_path, validation_report).

    Raises OllamaUnavailableError (daemon down) or RuntimeError (docx)."""
    jd_requirements = extract_jd_requirements(jd_text or "", model)
    content, validation_report = build_tailored_resume(
        profile, job, jd_requirements, model)

    safe_company = re.sub(r'[^\w]', '_', (job.get('company') or 'company').lower())
    safe_title = re.sub(r'[^\w]', '_', (job.get('title') or 'role').lower())[:30]
    output_path = DATA_DIR / f"resume_{safe_company}_{safe_title}.docx"
    _write_docx(content, output_path)
    return output_path, validation_report


def main():
    parser = argparse.ArgumentParser(description="Tailor resume to a specific ranked job")
    parser.add_argument("--rank", type=int, default=1, help="Rank of the job (1=top match)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Override the provider's default model "
                             "(Ollama locally, Groq when GROQ_API_KEY is set)")
    args = parser.parse_args()

    # Load real data
    with open(PROFILE_FILE, "r", encoding="utf-8") as f:
        profile = json.load(f)
    with open(DATA_DIR / "jobs.json", "r", encoding="utf-8") as f:
        jobs = json.load(f)

    # Prefer the saved ranked list (written by run_real_ranking); only
    # re-rank inline if it doesn't exist yet.
    if RANKED_FILE.exists():
        print(f"Loading ranked list from {RANKED_FILE}...")
        with open(RANKED_FILE, "r", encoding="utf-8") as f:
            ranked = json.load(f)
    else:
        print("No saved ranked list found -- ranking inline (this may take a moment)...")
        print("(Tip: run `python -m backend.job_scraper.run_real_ranking` to save one.)")
        from ..matching_engine.matcher import rank_jobs
        ranked = rank_jobs(profile, jobs, candidate_is_fresher=True)

    if args.rank < 1 or args.rank > len(ranked):
        print(f"ERROR: rank must be between 1 and {len(ranked)}")
        sys.exit(1)

    job = ranked[args.rank - 1]

    # ranked output doesn't carry clean_description -- look it up from jobs.json
    # using the canonical key (company + normalized title)
    title_norm = " ".join((job.get("title") or "").lower().split())
    company = (job.get("company") or "").lower().strip()
    full_job = next(
        (j for j in jobs
         if (j.get("company") or "").lower().strip() == company
         and " ".join((j.get("title") or "").lower().split()) == title_norm),
        None
    )
    if full_job:
        job["clean_description"] = full_job.get("clean_description", "")

    print(f"\nTailoring resume for: {job['title']} @ {job['company']}")
    print(f"Match score: {job['match_score']}")
    print(f"Strong matches: {job.get('strong_matches', [])[:5]}\n")

    print("Tailoring (JD analysis -> bullets -> validation -> docx)...")
    try:
        output_path, validation = tailor_to_job(
            profile, job, job.get("clean_description", ""), args.model)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    flagged = [v for v in validation if not v["ok"]]
    if flagged:
        print(f"\nNote: {len(flagged)} project(s) fell back to original "
              "bullets (fabrications caught by the validator).")
    print(f"\nDone! Resume saved to: {output_path}")
    print("Open in Word, review the tailored bullets, then submit.")


if __name__ == "__main__":
    try:
        main()
    except OllamaUnavailableError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)