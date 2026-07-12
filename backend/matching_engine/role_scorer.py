def compute_role_score(job):

    title = job.get(
        "title",
        ""
    ).lower()

    description = job.get(
        "clean_description",
        ""
    ).lower()

    score = 0

    # -----------------------------------
    # STRONG POSITIVE ROLES
    # -----------------------------------
    strong_roles = [
        "ai engineer",
        "machine learning engineer",
        "ml engineer",
        "data scientist",
        "llm engineer",
        "nlp engineer",
        "computer vision engineer",
        "deep learning engineer",
        "generative ai engineer",
        "applied scientist",
        "research scientist"
    ]

    for role in strong_roles:

        if role in title:
            score += 10

    # -----------------------------------
    # MEDIUM POSITIVE ROLES
    # -----------------------------------
    medium_roles = [
        "software engineer",
        "software developer",
        "backend engineer",
        "python developer",
        "data engineer"
    ]

    for role in medium_roles:

        if role in title:
            score += 3

    # -----------------------------------
    # NEGATIVE ROLES
    # -----------------------------------
    negative_roles = [
        "manager",
        "marketing",
        "sales",
        "trainer",
        "specialist",
        "operations",
        "recruiter",
        "seo",
        "account",
        "government",
        "legal",
        "tax"
    ]

    for role in negative_roles:

        if role in title:
            score -= 10

    # -----------------------------------
    # AI TECH STACK SIGNALS
    # -----------------------------------
    ai_keywords = [
        "machine learning",
        "deep learning",
        "llm",
        "rag",
        "transformer",
        "nlp",
        "computer vision",
        "pytorch",
        "tensorflow",
        "huggingface",
        "artificial intelligence",
        "generative ai"
    ]

    for keyword in ai_keywords:

        if keyword in description:
            score += 1

    return score