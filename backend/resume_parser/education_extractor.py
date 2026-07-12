import re

# Lines that are plausibly an institution NAME must contain one of these --
# grabbing an arbitrary nearby line produced garbage (live parse failure:
# institution extracted as "Docker command.", the tail of the preceding
# project bullet).
INSTITUTION_RE = re.compile(
    r'\b(institute|university|college|school|academy|polytechnic|iit|nit|iiit|bits)\b',
    re.IGNORECASE)

SECTION_LINE_RE = re.compile(
    r'^(education|projects?|skills|experience|certifications?|achievements?|'
    r'awards|publications|summary|objective)$', re.IGNORECASE)

EDUCATION_KEYWORDS = [
    "b.e", "btech", "b.tech", "m.tech", "mba", "bsc", "msc", "bca", "mca", "phd",
]


def _find_college_on_line(line, degree_keywords_lower):
    """Institution and degree often share one line, e.g.
    'Acharya Institute of Technology — B.E. Information Science'.
    Split on common separators; the institution part contains an
    institution keyword and no degree keyword."""
    parts = re.split(r'\s+[—–|]\s+|\s*\|\s*|,\s{2,}', line)
    college = None
    degree = None
    for part in parts:
        p = part.strip()
        if not p:
            continue
        lower = p.lower()
        has_degree_kw = any(kw in lower for kw in degree_keywords_lower)
        if has_degree_kw and degree is None:
            degree = p
        elif INSTITUTION_RE.search(p) and college is None:
            college = p
    return college, degree


def extract_education(text):
    education_data = []
    lines = [line.strip() for line in text.split('\n') if line.strip()]

    for i, line in enumerate(lines):
        lower_line = line.lower()
        if not any(keyword in lower_line for keyword in EDUCATION_KEYWORDS):
            continue

        # ---- degree + same-line college -----------------------------------
        college, degree_part = _find_college_on_line(line, EDUCATION_KEYWORDS)
        degree = degree_part or line
        degree = re.sub(r'\|\s*cgpa.*', '', degree, flags=re.IGNORECASE).strip()

        # ---- neighbor scan (only if same-line split found nothing) --------
        # Requires an institution keyword; skips section headers and lines
        # with years -- never grabs arbitrary prose again.
        if college is None:
            for j in range(max(0, i - 2), min(len(lines), i + 3)):
                if j == i:
                    continue
                candidate = lines[j]
                if SECTION_LINE_RE.match(candidate):
                    continue
                if re.search(r'20\d{2}', candidate):
                    continue
                if INSTITUTION_RE.search(candidate):
                    college = candidate
                    break

        # ---- CGPA + years from the surrounding window ---------------------
        nearby_text = " ".join(lines[max(0, i - 2): i + 3])

        cgpa = None
        cgpa_match = re.search(r'[cs]?gpa[: ]*([0-9]+(?:\.[0-9]+)?)',
                               nearby_text, re.IGNORECASE)
        if cgpa_match:
            cgpa = cgpa_match.group(1)

        years = None
        year_matches = list(dict.fromkeys(re.findall(r'20\d{2}', nearby_text)))
        if len(year_matches) >= 2:
            years = f"{year_matches[0]} - {year_matches[1]}"

        education_data.append({
            "college": college,
            "degree": degree,
            "cgpa": cgpa,
            "years": years,
        })

    return education_data
