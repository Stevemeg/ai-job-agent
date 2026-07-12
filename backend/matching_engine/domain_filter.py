from .role_scorer import compute_role_score


# -----------------------------------
# DETECT CANDIDATE DOMAINS
# -----------------------------------
def detect_candidate_domains(candidate_profile):

    skills = [
        skill["skill"].lower()
        for skill in candidate_profile.get(
            "skills",
            []
        )
    ]

    projects_text = " ".join([
        project["title"] + " " +
        project["description"]
        for project in candidate_profile.get(
            "projects",
            []
        )
    ]).lower()

    combined_text = " ".join(skills) + " " + projects_text

    domains = []

    # -----------------------------------
    # AI / ML
    # -----------------------------------
    ai_keywords = [
        "machine learning",
        "deep learning",
        "tensorflow",
        "pytorch",
        "llm",
        "rag",
        "nlp",
        "computer vision",
        "transformer",
        "generative ai",
        "huggingface",
        "faiss",
        "sentence transformer"
    ]

    if any(keyword in combined_text for keyword in ai_keywords):
        domains.append("ai_ml")

    # -----------------------------------
    # DATA SCIENCE
    # -----------------------------------
    data_keywords = [
        "pandas",
        "numpy",
        "analytics",
        "data analysis",
        "visualization"
    ]

    if any(keyword in combined_text for keyword in data_keywords):
        domains.append("data_science")

    # -----------------------------------
    # WEB DEVELOPMENT
    # -----------------------------------
    web_keywords = [
        "react",
        "node",
        "javascript",
        "frontend",
        "backend",
        "full stack",
        "django",
        "flask"
    ]

    if any(keyword in combined_text for keyword in web_keywords):
        domains.append("web_dev")

    return domains


# -----------------------------------
# FILTER JOBS BY DOMAIN
# -----------------------------------
def filter_jobs_by_domain(
    candidate_profile,
    jobs
):

    candidate_domains = detect_candidate_domains(
        candidate_profile
    )

    print(
        f"\nDetected Candidate Domains: "
        f"{candidate_domains}\n"
    )

    filtered_jobs = []

    # -----------------------------------
    # REJECT LIST
    # -----------------------------------
    reject_roles = [
        "project manager",
        "product manager",
        "sales engineer",
        "sales",
        "seo",
        "quality manager",
        "qa",
        "trainer",
        "recruiter",
        "marketing",
        "customer support",
        "account manager"
    ]

    for job in jobs:

        title = job.get(
            "title",
            ""
        ).lower()

        # Hard rejection
        if any(
            reject in title
            for reject in reject_roles
        ):
            continue

        role_score = compute_role_score(
            job
        )

        # -----------------------------------
        # AI / ML
        # -----------------------------------
        if "ai_ml" in candidate_domains:

            if role_score >= 2:
                filtered_jobs.append(job)

        # -----------------------------------
        # DATA SCIENCE
        # -----------------------------------
        elif "data_science" in candidate_domains:

            if role_score >= 1:
                filtered_jobs.append(job)

        # -----------------------------------
        # WEB DEV
        # -----------------------------------
        elif "web_dev" in candidate_domains:

            if role_score >= 1:
                filtered_jobs.append(job)

        # -----------------------------------
        # FALLBACK
        # -----------------------------------
        else:

            filtered_jobs.append(job)

    return filtered_jobs