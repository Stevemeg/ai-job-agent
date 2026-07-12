"""
cover_letter.py -- Ollama-powered cover letter generation.

Same pipeline shape as resume_tailor.py: pick a ranked job, extract JD
requirements via Mistral, then draft a short cover letter grounded ONLY in
the candidate's real profile. Every paragraph passes through the same
hallucination validator used for tailored resumes -- flagged paragraphs are
reported so you can review before sending.

Run from project root:
    python -m backend.resume_engine.cover_letter --rank 1
    python -m backend.resume_engine.cover_letter --rank 3 --txt-only
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
from .resume_tailor import (call_ollama, extract_jd_requirements,
                            DEFAULT_MODEL, OllamaUnavailableError)
from .validate_tailored import validate_bullet

DOCX_SCRIPT = Path(__file__).resolve().parent / "generate_cover_letter_docx.js"


def _profile_evidence_text(profile):
    """Everything the candidate can truthfully claim, as one search string."""
    parts = [profile.get("name", "")]
    parts += [s["skill"] for s in profile.get("skills", [])]
    for e in profile.get("education", []):
        parts += [e.get("college", ""), e.get("degree", ""), e.get("years", ""),
                  e.get("cgpa", "")]
    for p in profile.get("projects", []):
        parts += [p.get("title", ""), p.get("description", "")]
    return " ".join(parts)


def draft_cover_letter(profile, job, jd_requirements, model):
    """Ask Mistral for 3 short body paragraphs grounded in the real profile."""
    projects_text = "\n".join(
        f"- {p['title']}: {p['description'][:400]}"
        for p in profile.get("projects", []))
    skills_text = ", ".join(s["skill"] for s in profile.get("skills", []))
    role_summary = (jd_requirements or {}).get("role_summary", "")
    must_have = (jd_requirements or {}).get("must_have_skills", [])

    prompt = f"""You are an expert career coach writing a cover letter body for a new
graduate applying to a specific role. Return ONLY valid JSON.

STRICT RULES -- violations make the letter fraudulent:
1. Use ONLY skills, projects, metrics, and facts listed below. Do NOT add any
   tool, platform, company, or achievement that is not listed.
2. Do NOT claim professional work experience -- the candidate is a graduating
   student; their evidence is projects.
3. Keep it specific and concrete: name the actual projects and real metrics.
4. No cliches like "I am writing to express my interest" -- open with substance.

Target role: {job.get('title', '')} at {job.get('company', '')}
Role summary: {role_summary}
Role must-have skills: {json.dumps(must_have)}

Candidate skills: {skills_text}

Candidate projects (the ONLY permitted evidence):
{projects_text}

Write 3 short paragraphs (2-3 sentences each):
- paragraph 1: who the candidate is + the single strongest reason they fit THIS role
- paragraph 2: 1-2 specific projects with real metrics that prove the fit
- paragraph 3: brief close -- what they'd bring, availability, thanks

Return this exact JSON structure:
{{
  "paragraphs": ["paragraph 1", "paragraph 2", "paragraph 3"]
}}"""
    result = call_ollama(prompt, model)
    if result and isinstance(result.get("paragraphs"), list):
        return [p for p in result["paragraphs"] if isinstance(p, str) and p.strip()]
    return None


def validate_letter(paragraphs, profile):
    """Run each paragraph through the hallucination validator.

    Company/role names are allowed (the letter must mention them), so they're
    added to the allowed list by the caller. Returns list of per-paragraph
    results.
    """
    evidence = _profile_evidence_text(profile)
    skills = [s["skill"] for s in profile.get("skills", [])]
    return [validate_bullet(p, evidence, skills) for p in paragraphs]


def generate_for_job(profile, job, jd_text, model=DEFAULT_MODEL,
                     make_docx=True):
    """Full cover-letter pipeline for one job -- entry point for the UI and
    future API. Returns (docx_or_txt_path, paragraphs, review) where review
    is a list of {"paragraph": n, "flags": [...]} for unverified claims
    (company/role mentions excluded -- a letter must name them).

    Raises OllamaUnavailableError or RuntimeError."""
    jd_requirements = extract_jd_requirements(jd_text or "", model)
    paragraphs = draft_cover_letter(profile, job, jd_requirements, model)
    if not paragraphs:
        raise RuntimeError("The model did not return usable paragraphs -- "
                           "try again (LLM output is non-deterministic).")

    expected = {(job.get("company") or "").lower(), *(
        w.lower() for w in re.findall(r"[A-Za-z]+", job.get("title") or ""))}
    review = []
    for i, r in enumerate(validate_letter(paragraphs, profile), 1):
        flags = ([t for t in r["flagged_terms"] if t.lower() not in expected]
                 + r["flagged_numbers"])
        if flags:
            review.append({"paragraph": i, "flags": flags})

    safe_company = re.sub(r'[^\w]', '_', (job.get('company') or 'company').lower())
    safe_title = re.sub(r'[^\w]', '_', (job.get('title') or 'role').lower())[:30]

    letter_lines = [profile.get("name", ""), profile.get("email", ""),
                    profile.get("phone", ""), "",
                    f"Re: {job.get('title', '')} -- {job.get('company', '')}",
                    "", "Dear Hiring Team,", ""]
    for p in paragraphs:
        letter_lines += [p, ""]
    letter_lines += ["Sincerely,", profile.get("name", "")]
    txt_path = DATA_DIR / f"cover_letter_{safe_company}_{safe_title}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(letter_lines))

    if not make_docx:
        return txt_path, paragraphs, review

    payload = {
        "candidate": {"name": profile.get("name", ""),
                      "email": profile.get("email", ""),
                      "phone": profile.get("phone", ""),
                      "linkedin": profile.get("linkedin", "")},
        "target_role": job.get("title", ""),
        "target_company": job.get("company", ""),
        "paragraphs": paragraphs,
    }
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json',
                                     delete=False, encoding='utf-8') as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp_path = tmp.name
    docx_path = DATA_DIR / f"cover_letter_{safe_company}_{safe_title}.docx"
    try:
        result = subprocess.run(
            ["node", str(DOCX_SCRIPT), tmp_path, str(docx_path)],
            capture_output=True, text=True)
    except FileNotFoundError:
        return txt_path, paragraphs, review        # no node -> .txt still valid
    finally:
        os.unlink(tmp_path)
    if result.returncode != 0:
        return txt_path, paragraphs, review
    return docx_path, paragraphs, review


def main():
    parser = argparse.ArgumentParser(description="Generate a tailored cover letter")
    parser.add_argument("--rank", type=int, default=1, help="Rank of the job (1=top match)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Override the provider's default model "
                             "(Ollama locally, Groq when GROQ_API_KEY is set)")
    parser.add_argument("--txt-only", action="store_true",
                        help="Skip .docx generation, write .txt only")
    args = parser.parse_args()

    with open(PROFILE_FILE, "r", encoding="utf-8") as f:
        profile = json.load(f)
    with open(DATA_DIR / "jobs.json", "r", encoding="utf-8") as f:
        jobs = json.load(f)

    if RANKED_FILE.exists():
        print(f"Loading ranked list from {RANKED_FILE}...")
        with open(RANKED_FILE, "r", encoding="utf-8") as f:
            ranked = json.load(f)
    else:
        print("No saved ranked list found -- ranking inline (this may take a moment)...")
        from ..matching_engine.matcher import rank_jobs
        ranked = rank_jobs(profile, jobs, candidate_is_fresher=True)

    if args.rank < 1 or args.rank > len(ranked):
        print(f"ERROR: rank must be between 1 and {len(ranked)}")
        sys.exit(1)
    job = ranked[args.rank - 1]

    # Look up the full JD text (ranked entries don't carry it)
    title_norm = " ".join((job.get("title") or "").lower().split())
    company = (job.get("company") or "").lower().strip()
    full_job = next(
        (j for j in jobs
         if (j.get("company") or "").lower().strip() == company
         and " ".join((j.get("title") or "").lower().split()) == title_norm),
        None)
    jd_text = full_job.get("clean_description", "") if full_job else ""

    print(f"\nCover letter for: {job['title']} @ {job['company']}")

    print("Step 1/3: Extracting JD requirements via Ollama...")
    jd_requirements = extract_jd_requirements(jd_text, args.model)

    print("Step 2/3: Drafting cover letter...")
    paragraphs = draft_cover_letter(profile, job, jd_requirements, args.model)
    if not paragraphs:
        print("ERROR: Ollama did not return usable paragraphs. Try again or use --model.")
        sys.exit(1)

    # Truthfulness review -- report, don't auto-rewrite. A cover letter
    # legitimately mentions the company/role, so those terms are expected
    # flags; everything else deserves a human look.
    print("\nTruthfulness check (company/role mentions are expected flags):")
    expected = {job.get("company", "").lower(), *(
        w.lower() for w in re.findall(r"[A-Za-z]+", job.get("title", "")))}
    any_real_flag = False
    for i, r in enumerate(validate_letter(paragraphs, profile), 1):
        real_flags = [t for t in r["flagged_terms"] if t.lower() not in expected]
        if real_flags or r["flagged_numbers"]:
            any_real_flag = True
            print(f"  Paragraph {i}: REVIEW -- unverified terms {real_flags}, "
                  f"numbers {r['flagged_numbers']}")
        else:
            print(f"  Paragraph {i}: OK")
    if any_real_flag:
        print("  -> Edit the flagged claims before sending.")

    # Assemble full letter text
    letter_lines = [
        profile.get("name", ""),
        profile.get("email", ""),
        profile.get("phone", ""),
        "",
        f"Re: {job.get('title', '')} -- {job.get('company', '')}",
        "",
        "Dear Hiring Team,",
        "",
    ]
    for p in paragraphs:
        letter_lines += [p, ""]
    letter_lines += ["Sincerely,", profile.get("name", "")]
    letter_text = "\n".join(letter_lines)

    safe_company = re.sub(r'[^\w]', '_', job.get('company', 'company').lower())
    safe_title = re.sub(r'[^\w]', '_', job.get('title', 'role').lower())[:30]

    txt_path = DATA_DIR / f"cover_letter_{safe_company}_{safe_title}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(letter_text)
    print(f"\nStep 3/3: Saved {txt_path}")

    if not args.txt_only:
        payload = {
            "candidate": {
                "name": profile.get("name", ""),
                "email": profile.get("email", ""),
                "phone": profile.get("phone", ""),
                "linkedin": profile.get("linkedin", ""),
            },
            "target_role": job.get("title", ""),
            "target_company": job.get("company", ""),
            "paragraphs": paragraphs,
        }
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json',
                                         delete=False, encoding='utf-8') as tmp:
            json.dump(payload, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        docx_path = DATA_DIR / f"cover_letter_{safe_company}_{safe_title}.docx"
        result = subprocess.run(
            ["node", str(DOCX_SCRIPT), tmp_path, str(docx_path)],
            capture_output=True, text=True)
        os.unlink(tmp_path)
        if result.returncode != 0:
            print(f"WARNING: docx generation failed: {result.stderr}")
            print("(.txt version was still saved above)")
        else:
            print(f"Saved {docx_path}")

    print("\nDone. Review the truthfulness flags above before sending.")


if __name__ == "__main__":
    try:
        main()
    except OllamaUnavailableError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
