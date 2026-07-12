"""
requirement_extractor.py -- Phase 3: structured requirement extraction.

Replaces scattered, blunt regexes (scorer._required_years,
job_parser.extract_experience) with ONE function that parses a job
description into a structured dict, which both the hard-filter layer and
the scorer can consume.

Why this matters concretely: the OLD regex grabbed the first number near
the word "years" and applied the same hard cutoff regardless of context --
"5+ years required" and "3-5 years preferred" were treated identically.
This extractor distinguishes REQUIRED from PREFERRED, and only hard-gates
on the former. For a range like "3-5 years", the upper bound (5) is
reported, since that's the more informative number for judging fit against
a role's real ceiling.
"""
import re

# Window of characters scanned around a number match to decide whether the
# surrounding language signals "required" or "preferred". Kept tight (60
# chars ~ 8-10 words) so we're reading the actual clause, not the whole
# paragraph -- a "preferred" two sentences away shouldn't soften a separate,
# genuinely hard requirement stated earlier.
CONTEXT_WINDOW = 60

PREFERRED_SIGNALS = (
    "preferred", "nice to have", "a plus", "bonus", "ideally",
    "or equivalent", "desired", "advantageous",
)
REQUIRED_SIGNALS = (
    "required", "must have", "minimum", "at least", "no exceptions",
    "mandatory", "essential",
)
# Company-age boilerplate ("founded 12 years ago", "in business for 20
# years") matches the same \d+ years pattern as an experience requirement
# but means something completely different. Found via real data: a job
# wanting only "1 year preferred" was wrongly gated out because its
# description also said "Founded 12 years ago" elsewhere in the text.
#
# Deliberately checked in a TIGHT window immediately around the match (not
# the wider CONTEXT_WINDOW used for preferred/required), since a company-age
# sentence elsewhere in the same description must not suppress a separate,
# genuine requirement sentence nearby -- only suppress when the trigger word
# sits immediately adjacent to THIS specific number.
COMPANY_AGE_WINDOW = 20
COMPANY_AGE_BEFORE = ("founded", "established", "in business", "since", "celebrating")
COMPANY_AGE_AFTER = ("ago", "anniversary")

YEARS_PATTERN = re.compile(
    r'(\d+)\+?\s*(?:-\s*(\d+)\s*)?\s*(?:years?|yrs?)', re.IGNORECASE
)

CLEARANCE_PATTERN = re.compile(
    r'security clearance|active clearance|TS/SCI|polygraph', re.IGNORECASE
)
NO_SPONSORSHIP_PATTERN = re.compile(
    r'without sponsorship|not (?:able|eligible) to sponsor|'
    r'no visa sponsorship|must be (?:a |an )?(?:u\.?s\.?|US) citizen',
    re.IGNORECASE
)


def _context_around(text, match):
    start = max(0, match.start() - CONTEXT_WINDOW)
    end = min(len(text), match.end() + CONTEXT_WINDOW)
    return text[start:end].lower()


def extract_years_requirement(description):
    """Returns (years: int|None, is_strict_requirement: bool).

    is_strict_requirement is False whenever a PREFERRED-style signal sits in
    the same local context as the number -- in that case years is still
    returned (useful for display/scoring) but should NOT be used to hard-gate
    the candidate out.
    """
    if not description:
        return None, False

    best_years = None
    best_strict = False

    for match in YEARS_PATTERN.finditer(description):
        # Tight, LOCAL check: does a company-age trigger word sit immediately
        # before or after THIS specific number (not just somewhere in the
        # wider sentence/paragraph)? This must stay narrow so a company-age
        # mention elsewhere in the description doesn't suppress a separate,
        # genuine requirement sentence nearby.
        before = description[max(0, match.start() - COMPANY_AGE_WINDOW):match.start()].lower()
        after = description[match.end():match.end() + COMPANY_AGE_WINDOW].lower()
        if (any(sig in before for sig in COMPANY_AGE_BEFORE)
                or any(sig in after for sig in COMPANY_AGE_AFTER)):
            continue

        # group(1) is the first/lower number; group(2), if present, is the
        # upper bound of a range like "3-5 years" -- prefer it when present,
        # since it's the more informative number for judging fit.
        years = int(match.group(2)) if match.group(2) else int(match.group(1))
        context = _context_around(description, match)

        has_preferred = any(sig in context for sig in PREFERRED_SIGNALS)
        has_required = any(sig in context for sig in REQUIRED_SIGNALS)

        # A number with no nearby signal either way defaults to strict --
        # matches the conservative behavior of the OLD regex, so we don't
        # accidentally become MORE permissive than before on plain JDs like
        # "5+ years of experience with Python" (no preferred/required word
        # at all, but plainly meant as a requirement).
        is_strict = has_required or not has_preferred

        # Track the LARGEST years number seen (a JD listing "2 years with
        # Python, 5 years with distributed systems" should gate on the
        # bigger ask), but a strict number always overrides into being the
        # one reported if found.
        if best_years is None or years > best_years or (is_strict and not best_strict):
            best_years = years
            best_strict = is_strict

    return best_years, best_strict


def extract_requirements(description):
    """Full structured extraction for one job. Returns a dict consumed by
    both the hard-filter layer (scorer.passes_hard_filters) and scoring."""
    description = description or ""

    years, years_strict = extract_years_requirement(description)

    return {
        "years_required": years,
        "years_is_strict": years_strict,
        "requires_clearance": bool(CLEARANCE_PATTERN.search(description)),
        "no_sponsorship": bool(NO_SPONSORSHIP_PATTERN.search(description)),
    }