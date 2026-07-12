import re
from bs4 import BeautifulSoup


# -----------------------------------
# CLEAN HTML DESCRIPTION
# -----------------------------------
def clean_job_description(html_text):

    if not html_text:
        return ""

    soup = BeautifulSoup(html_text, "html.parser")
    text = soup.get_text(separator=" ")

    text = text.replace("â", "")
    text = text.replace("€", "")
    text = text.replace("™", "")

    text = re.sub(r'\s+', ' ', text)

    return text.strip()


# -----------------------------------
# EXTRACT EXPERIENCE
# -----------------------------------
def extract_experience(text):

    pattern = r'(\d+)\+?\s*(years|year)'
    match = re.search(pattern, text, re.IGNORECASE)

    if match:
        return match.group()

    return None


# -----------------------------------
# DETECT SENIORITY
# -----------------------------------
def detect_seniority(title):
    """Order matters: check most-senior / most-restrictive titles FIRST.
    'Senior Staff Engineer' should land in Staff, not get caught early by
    'senior'. Likewise 'PhD ... Intern' needs its own bucket checked BEFORE
    the generic 'intern' branch -- a PhD-track research internship requires
    active PhD enrollment, which most undergrad/fresher candidates don't
    have, even though the literal word 'intern' is also in the title."""

    title = title.lower()

    # PhD-track roles (often titled as "intern" or "research scientist") have
    # a hard eligibility bar most freshers don't clear. Must be checked before
    # the "intern" branch below, or "PhD ... Intern" silently becomes the
    # most fresher-friendly bucket instead of one of the least accessible.
    if "phd" in title or "ph.d" in title:
        return "PhD"

    # Principal / Staff sit ABOVE Senior on most IC ladders. Originally
    # missing entirely, which silently defaulted these to "Mid-Level".
    elif "principal" in title:
        return "Principal"

    elif "staff" in title:
        return "Staff"

    elif "senior" in title or re.search(r'\bsr\b', title):
        return "Senior"

    elif "junior" in title or re.search(r'\bjr\b', title):
        return "Junior"

    elif "lead" in title:
        return "Lead"

    elif "intern" in title:
        return "Intern"

    return "Mid-Level"


# -----------------------------------
# PARSE JOB
# -----------------------------------
def parse_job(job):

    cleaned_description = clean_job_description(
        job.get("description", "")
    )

    parsed_job = {
        "title": job.get("title"),
        "company": job.get("company"),
        "location": job.get("location"),
        "tags": job.get("tags", []),
        "experience": extract_experience(
            cleaned_description
        ),
        "seniority": detect_seniority(
            job.get("title", "")
        ),
        "clean_description": cleaned_description,
        "apply_link": job.get("apply_link")
    }

    return parsed_job