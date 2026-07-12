import re

# A line is "mostly a duration" if, after removing a date range, almost nothing is left.
DURATION_PATTERN = re.compile(
    r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{0,2},?\s*20\d{2}'
    r'\s*[–—\-]\s*'
    r'((Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{0,2},?\s*20\d{2}|present|current|now|ongoing)',
    re.IGNORECASE,
)

# Real resumes use many bullet glyphs. CRITICAL SUBTLETY: Word documents
# with Wingdings/Symbol bullets (incl. checkmarks) export PRIVATE-USE
# codepoints (U+F000-U+F8FF, e.g. U+F0FC for the Wingdings checkmark) --
# they LOOK like a checkmark in a PDF viewer but are not U+2713. Both live
# parse failures came from bullet glyphs this regex didn't know.
BULLET_RE = re.compile(
    r'^\s*(?:'
    r'[•●▪■○✓✔➤→◦‣·⁃]\s*'
    r'|[-]\s*'      # private-use: Wingdings/Symbol PDF exports
    r'|[*\-–]\s+'          # ascii dash/star bullets require a space
    r')')

SECTION_HEADERS = ("certification", "certificate", "education", "experience",
                   "skills", "achievements", "awards", "publications")


def _is_bullet(line):
    return bool(BULLET_RE.match(line))


def _is_title_like(line):
    """Heuristic separating project TITLES from wrapped bullet continuations.

    Titles: short, start with an uppercase letter/digit, no sentence-ending
    punctuation. Continuations: start lowercase or end with '.'."""
    if not line or len(line) > 90:
        return False
    if line.rstrip().endswith((".", ",", ";", ":")):
        return False
    first = line.lstrip()[:1]
    if not first.isalnum() or first.islower():
        return False
    if len(line.split()) > 12:
        return False
    return True


def _clean_title(line):
    # strip trailing LINK markers, any inline duration, and bracketed link hints
    line = DURATION_PATTERN.sub("", line)
    line = re.sub(r'\bLINK\b', "", line, flags=re.IGNORECASE)
    line = re.sub(r'\s{2,}', " ", line)
    return line.strip(" |-–—\t")


def extract_projects(text):
    """Robust project extractor. Handles three real layouts:

    1. Title and duration on separate lines (title \\n Mon YYYY - Mon YYYY \\n bullets)
    2. Inline title + duration on one line
    3. NO durations at all: title-like line followed by bullet lines
    """
    m = re.search(
        r'^\s*projects?\s*$(.*?)(?=^\s*(?:%s)s?\s*$|\Z)' % "|".join(SECTION_HEADERS),
        text, re.DOTALL | re.IGNORECASE | re.MULTILINE,
    )
    section = m.group(1) if m else ""
    if not section.strip():
        m2 = re.search(r'projects?(.*)', text, re.DOTALL | re.IGNORECASE)
        section = m2.group(1) if m2 else ""
    if not section.strip():
        return []

    lines = [ln.strip() for ln in section.split("\n") if ln.strip()]

    projects = []
    current = None
    prev_title_candidate = None  # last title-like, non-bullet line seen

    for line in lines:
        has_duration = bool(DURATION_PATTERN.search(line))
        title_after_removing_duration = _clean_title(line) if has_duration else line

        if has_duration and not title_after_removing_duration:
            # DURATION-ONLY line -> the title is the previous candidate line.
            title = _clean_title(prev_title_candidate) if prev_title_candidate else ""
            current = {"title": title,
                       "duration": DURATION_PATTERN.search(line).group().strip(),
                       "description": ""}
            projects.append(current)
            prev_title_candidate = None

        elif has_duration and title_after_removing_duration:
            # INLINE: title + duration on the same line.
            current = {"title": title_after_removing_duration,
                       "duration": DURATION_PATTERN.search(line).group().strip(),
                       "description": ""}
            projects.append(current)
            prev_title_candidate = None

        elif _is_bullet(line):
            # A pending title candidate followed by a bullet = a new project
            # even WITHOUT a duration line (layout 3).
            if prev_title_candidate is not None:
                current = {"title": _clean_title(prev_title_candidate),
                           "duration": "", "description": ""}
                projects.append(current)
                prev_title_candidate = None
            elif current is None:
                current = {"title": "", "duration": "", "description": ""}
                projects.append(current)
            desc = BULLET_RE.sub("", line).strip()
            current["description"] = (current["description"] + " • " + desc).strip()

        else:
            # Non-bullet, non-duration prose line.
            if _is_title_like(line):
                prev_title_candidate = line
            elif current is not None and current["description"]:
                current["description"] = (current["description"] + " " + line).strip()

    return [p for p in projects if p["title"] or p["description"]]
