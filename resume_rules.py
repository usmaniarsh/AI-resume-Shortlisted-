"""
Deterministic, non-AI checks for resume quality.
No external calls — pure Python so it's fast and free to run on every application.
"""
import re

WEAK_VERBS = {
    "worked", "helped", "responsible", "involved", "assisted",
    "handled", "did", "participated", "supported",
}

STRONG_VERB_HINTS = {
    "led", "built", "designed", "developed", "implemented", "automated",
    "optimized", "reduced", "increased", "launched", "managed", "created",
    "improved", "delivered", "architected", "drove", "owned",
}


def check_action_verbs(resume_text: str) -> dict:
    """Flag bullet/line starts that use weak verbs instead of strong action verbs."""
    lines = [l.strip() for l in resume_text.split("\n") if l.strip()]
    weak_hits = 0
    strong_hits = 0
    for line in lines:
        first_word = re.sub(r"^[•\-\*\d\.\)\s]+", "", line).split(" ")[0].lower()
        first_word = re.sub(r"[^a-z]", "", first_word)
        if first_word in WEAK_VERBS:
            weak_hits += 1
        elif first_word in STRONG_VERB_HINTS:
            strong_hits += 1

    flags = []
    if weak_hits > 0:
        flags.append(f"{weak_hits} line(s) start with weak verbs (e.g. 'worked', 'helped').")
    return {
        "weak_verb_count": weak_hits,
        "strong_verb_count": strong_hits,
        "flags": flags,
    }


def check_keyword_overlap(resume_text: str, job_requirements: str, top_n: int = 8) -> dict:
    """Very simple keyword overlap: pulls capitalized/technical-looking tokens from the
    job requirements and checks whether they appear in the resume text."""
    resume_lower = resume_text.lower()

    # crude keyword extraction: words/phrases that look like tools/tech (capitalized,
    # contain digits, or are short acronyms) plus common multi-word skill phrases.
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9\+\#\.]{1,20}", job_requirements)
    seen = set()
    keywords = []
    for word in candidates:
        w = word.strip(".")
        wl = w.lower()
        if len(wl) < 3 or wl in seen:
            continue
        # skip common english stop-ish words
        if wl in {"with", "and", "the", "for", "experience", "knowledge", "strong",
                   "hands", "related", "basic", "understanding", "best", "such", "associated"}:
            continue
        seen.add(wl)
        keywords.append(w)

    missing = [k for k in keywords if k.lower() not in resume_lower]
    present = [k for k in keywords if k.lower() in resume_lower]

    missing = missing[:top_n]
    flags = []
    if missing:
        flags.append("Missing keywords from job requirements: " + ", ".join(missing))

    return {
        "matched_keywords": present[:top_n],
        "missing_keywords": missing,
        "flags": flags,
    }


def check_readability(resume_text: str) -> dict:
    """Flag overly long lines/paragraphs that hurt scanability."""
    lines = [l.strip() for l in resume_text.split("\n") if l.strip()]
    long_lines = [l for l in lines if len(l.split()) > 35]
    flags = []
    if long_lines:
        flags.append(f"{len(long_lines)} line(s)/paragraph(s) are very long (35+ words) — hard to scan.")
    return {
        "long_line_count": len(long_lines),
        "flags": flags,
    }


def run_rule_checks(resume_text: str, job_requirements: str = "") -> dict:
    """Run all deterministic checks and return a single merged result."""
    verbs = check_action_verbs(resume_text)
    readability = check_readability(resume_text)
    result = {
        "weak_verb_count": verbs["weak_verb_count"],
        "strong_verb_count": verbs["strong_verb_count"],
        "long_line_count": readability["long_line_count"],
        "rule_flags": verbs["flags"] + readability["flags"],
    }

    if job_requirements:
        keywords = check_keyword_overlap(resume_text, job_requirements)
        result["matched_keywords"] = keywords["matched_keywords"]
        result["missing_keywords"] = keywords["missing_keywords"]
        result["rule_flags"] += keywords["flags"]

    return result
