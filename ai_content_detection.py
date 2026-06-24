"""
AI-generated resume detection.

Heuristic, explainable detector — NOT a trained classifier (no model weights
needed, runs instantly, and every flag is something you can show the HR user
as "why we think this"). This is the same style used by most "AI text
detector" tools at the heuristic layer, before any ML scoring is layered on.

Signals used:
1. Burstiness / sentence-length variance — human writing varies sentence
   length more; GPT output tends to be more uniform.
2. Stock GPT-isms — phrases that LLMs overuse ("leverage", "spearheaded",
   "passionate about", "proven track record", "seamlessly", "robust",
   "synergy", "dynamic environment", "results-driven", etc.)
3. Repetition of rare connector phrases ("furthermore", "moreover",
   "in conclusion") which are common in LLM output but rare in resumes.
4. Perfect parallel-structure bullets (every bullet starts with the exact
   same tense/pattern) — slight signal, weighted low since strong resumes
   also do this intentionally.

This gives a 0-100 "AI-likelihood" score with human-readable reasons, not a
black-box verdict — important since false positives are costly (a genuinely
good human-written resume shouldn't get penalized).
"""
import re
import statistics

GPT_PHRASES = [
    "leverage", "leveraging", "spearheaded", "passionate about",
    "proven track record", "seamlessly", "robust", "synergy",
    "dynamic environment", "results-driven", "results driven",
    "fast-paced environment", "cutting-edge", "in today's world",
    "it is important to note", "furthermore", "moreover",
    "in conclusion", "holistic approach", "paradigm", "utilize",
    "facilitate", "streamline", "delve into", "underscore",
    "showcasing", "demonstrable", "unwavering commitment",
    "testament to", "navigate the complexities",
]


def _sentence_lengths(text: str) -> list[int]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [len(s.split()) for s in sentences if len(s.split()) > 2]


def _gpt_phrase_hits(text: str) -> list[str]:
    text_lower = text.lower()
    return [p for p in GPT_PHRASES if p in text_lower]


def _bullet_uniformity(text: str) -> float:
    """Fraction of bullet lines that start with the exact same tense pattern
    (e.g. every line starting with a past-tense verb) — mild signal only."""
    lines = [l.strip() for l in text.split("\n") if l.strip().startswith(("-", "•", "*"))]
    if len(lines) < 4:
        return 0.0
    first_words = [re.sub(r"^[-•\*\s]+", "", l).split(" ")[0].lower() for l in lines]
    ed_count = sum(1 for w in first_words if w.endswith("ed"))
    return ed_count / len(first_words)


def detect_ai_generated(resume_text: str) -> dict:
    reasons = []
    score = 0  # 0-100, higher = more likely AI-written

    # Signal 1: burstiness (low variance in sentence length => more AI-like)
    lengths = _sentence_lengths(resume_text)
    if len(lengths) >= 5:
        stdev = statistics.pstdev(lengths)
        mean = statistics.mean(lengths)
        cv = stdev / mean if mean else 0  # coefficient of variation
        if cv < 0.25:
            score += 30
            reasons.append("Very uniform sentence length (low variation) — common in LLM-generated text.")
        elif cv < 0.4:
            score += 15
            reasons.append("Somewhat uniform sentence length.")

    # Signal 2: GPT-isms / overused AI phrases
    hits = _gpt_phrase_hits(resume_text)
    if hits:
        weight = min(40, len(hits) * 8)
        score += weight
        reasons.append(f"Contains {len(hits)} commonly AI-overused phrase(s): {', '.join(hits[:5])}.")

    # Signal 3: bullet uniformity
    uniformity = _bullet_uniformity(resume_text)
    if uniformity > 0.85:
        score += 15
        reasons.append("Nearly all bullet points follow an identical grammatical pattern.")
    elif uniformity > 0.6:
        score += 7

    score = min(100, score)

    if score >= 60:
        verdict = "Likely AI-generated or heavily AI-edited"
    elif score >= 30:
        verdict = "Possibly AI-assisted"
    else:
        verdict = "Likely human-written"

    if not reasons:
        reasons.append("No strong AI-writing signals detected.")

    return {
        "ai_likelihood_score": score,
        "verdict": verdict,
        "reasons": reasons,
    }
