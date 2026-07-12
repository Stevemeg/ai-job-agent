import fitz

from .text_cleaner import clean_resume_text

from .skill_extractor import (
    load_skills_database,
    extract_skills
)

from .entity_extractor import (
    extract_name,
    extract_email,
    extract_phone
)

from .education_extractor import (
    extract_education
)

from .project_extractor import (
    extract_projects
)


# -----------------------------------
# PDF TEXT EXTRACTION
# -----------------------------------
def extract_text_from_pdf(pdf_path):

    full_text = ""

    try:
        doc = fitz.open(pdf_path)

        for page in doc:

            blocks = page.get_text("blocks")

            blocks = sorted(blocks, key=lambda b: b[1])

            for block in blocks:

                block_text = block[4].strip()

                if block_text:
                    full_text += block_text + "\n"

        return full_text

    except Exception as e:
        return f"Error: {e}"


# -----------------------------------
# PDF LINK EXTRACTION
# -----------------------------------
def extract_links_from_pdf(pdf_path):

    links = []

    try:
        doc = fitz.open(pdf_path)

        for page in doc:

            page_links = page.get_links()

            for link in page_links:

                uri = link.get("uri", None)

                if uri:
                    links.append(uri)

    except Exception as e:
        print(f"Link Extraction Error: {e}")

    return links


# -----------------------------------
# LINK CLASSIFICATION
# -----------------------------------
def classify_links(links):

    linkedin = None
    github = None

    for link in links:

        lower_link = link.lower()

        if "linkedin.com" in lower_link:
            linkedin = link

        elif "github.com" in lower_link:
            github = link

    return linkedin, github


# -----------------------------------
# BUILD PROFILE
# -----------------------------------
def build_candidate_profile(cleaned_text, pdf_links):

    skills_df = load_skills_database(
        "../datasets/skills_database.csv"
    )

    name = extract_name(cleaned_text)

    email = extract_email(cleaned_text)

    phone = extract_phone(cleaned_text)

    linkedin, github = classify_links(pdf_links)

    extracted_skills = extract_skills(
        cleaned_text,
        skills_df
    )

    education = extract_education(cleaned_text)

    projects = extract_projects(cleaned_text)

    candidate_profile = {
        "name": name,
        "email": email,
        "phone": phone,
        "linkedin": linkedin,
        "github": github,
        "skills": extracted_skills,
        "education": education,
        "projects": projects
    }

    return candidate_profile