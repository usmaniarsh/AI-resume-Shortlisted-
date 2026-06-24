"""
Blind / bias-free screening.

Strips identity-revealing signals from resume text BEFORE it goes to the AI
match-scoring step, so the score is based on skills/experience only.

Approach: regex + light heuristics (no external NLP download needed, so it
runs anywhere). For production-grade name detection you could later swap in
spaCy's NER (en_core_web_sm) — the redact_for_blind_screening() function is
written so that swap only touches one place.
"""
import re

# Common indicators of name lines on Indian/global resumes — first non-empty
# line is usually the candidate's name (header convention almost all resumes follow).
GENDER_WORDS = {
    "male", "female", "man", "woman", "mr", "mrs", "ms", "mx", "he", "she",
    "him", "her", "his", "hers", "transgender", "non-binary",
}

COLLEGE_TIER_WORDS = [
    "IIT", "NIT", "IIM", "BITS Pilani", "IIIT", "Ivy League", "Harvard",
    "Stanford", "MIT", "Oxford", "Cambridge",
]

AGE_PATTERNS = [
    r"\bage[:\s]*\d{1,2}\b",
    r"\bdate of birth\b.*",
    r"\bDOB\b.*",
    r"\b(19|20)\d{2}\s*[-–]\s*(present|current|(19|20)\d{2})\b",  # leave education years alone; only flags explicit DOB-style
]

PHOTO_MARKERS = ["[image]", "[photo]", "<img", "passport size photo"]


def _redact_name(text: str) -> tuple[str, list[str]]:
    """Best-effort: resumes conventionally open with the candidate's full name
    on its own line. Redact the first short, title-case line."""
    lines = text.split("\n")
    redactions = []
    for i, line in enumerate(lines[:5]):  # name almost always in first few lines
        stripped = line.strip()
        words = stripped.split()
        if 1 < len(words) <= 4 and all(w[:1].isupper() for w in words if w.isalpha()):
            # looks like "Mohd Arsh Usmani" — redact
            redactions.append(stripped)
            lines[i] = "[CANDIDATE NAME REDACTED]"
            break
    return "\n".join(lines), redactions


def _redact_gender(text: str) -> tuple[str, list[str]]:
    redactions = []
    def repl(m):
        redactions.append(m.group(0))
        return "[REDACTED]"
    pattern = r"\b(" + "|".join(re.escape(w) for w in GENDER_WORDS) + r")\b"
    new_text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return new_text, redactions


def _redact_college_tier(text: str) -> tuple[str, list[str]]:
    redactions = []
    def repl(m):
        redactions.append(m.group(0))
        return "[INSTITUTION REDACTED]"
    pattern = r"\b(" + "|".join(re.escape(w) for w in COLLEGE_TIER_WORDS) + r")\b"
    new_text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return new_text, redactions


def _redact_age(text: str) -> tuple[str, list[str]]:
    redactions = []
    new_text = text
    for pattern in AGE_PATTERNS[:2]:  # DOB/age lines only, skip the experience-years pattern
        def repl(m):
            redactions.append(m.group(0))
            return "[AGE/DOB REDACTED]"
        new_text = re.sub(pattern, repl, new_text, flags=re.IGNORECASE)
    return new_text, redactions


def _redact_photo_markers(text: str) -> tuple[str, list[str]]:
    redactions = []
    new_text = text
    for marker in PHOTO_MARKERS:
        if marker.lower() in new_text.lower():
            redactions.append(marker)
            new_text = re.sub(re.escape(marker), "[PHOTO REDACTED]", new_text, flags=re.IGNORECASE)
    return new_text, redactions


def _redact_email_phone(text: str) -> tuple[str, list[str]]:
    """Email/phone aren't bias signals, but they DO de-anonymize a candidate
    in a blind round, so strip them too for true blind screening."""
    redactions = []
    def repl_email(m):
        redactions.append(m.group(0))
        return "[EMAIL REDACTED]"
    text = re.sub(r"[\w\.-]+@[\w\.-]+\.\w+", repl_email, text)

    def repl_phone(m):
        redactions.append(m.group(0))
        return "[PHONE REDACTED]"
    text = re.sub(r"(\+?\d{1,3}[-\s]?)?\d{10}\b", repl_phone, text)
    return text, redactions


def redact_for_blind_screening(resume_text: str) -> dict:
    """Run all redaction passes. Returns the redacted text plus a transparency
    log of what was removed (useful to show HR: 'here's what we hid')."""
    text = resume_text
    log = {}

    text, log["name"] = _redact_name(text)
    text, log["gender"] = _redact_gender(text)
    text, log["college_tier"] = _redact_college_tier(text)
    text, log["age_dob"] = _redact_age(text)
    text, log["photo"] = _redact_photo_markers(text)
    text, log["contact"] = _redact_email_phone(text)

    redacted_count = sum(len(v) for v in log.values())

    return {
        "redacted_text": text,
        "redaction_log": log,
        "redacted_count": redacted_count,
    }
