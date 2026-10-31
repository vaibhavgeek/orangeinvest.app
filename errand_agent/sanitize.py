"""Treat provider free text as untrusted data: clean it, flag injection attempts, never execute it."""
import re

INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above|earlier) instructions",
    r"\bsystem\s*:",
    r"<\s*/?\s*(assistant|system|user|review|tool)\b",
    r"\b(ai|assistant|agent|model|chatbot)\b.{0,40}\b(must|should|note|instruction|important)",
    r"note to (the )?(ai|assistant|agent)",
    r"pre-?approved|approval granted|already (said yes|confirmed|approved)",
    r"\b(call|invoke|run|execute)\s+\w+\s*\(",
    r"\b(book|cancel|reschedule|hold_slot)\s*\(",
    r"skip (the )?(confirmation|approval)",
    r"remember that the user",
]
_INJ = [re.compile(p, re.I) for p in INJECTION_PATTERNS]
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e]")
_TAG = re.compile(r"<[^>]{0,40}>")


def looks_like_injection(text: str) -> bool:
    return any(p.search(text or "") for p in _INJ)


def clean_text(text: str, max_len: int = 200) -> str:
    t = _CTRL.sub("", str(text or ""))
    t = _TAG.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= max_len else t[: max_len - 1] + "…"


def sanitize_provider(details: dict) -> dict:
    """Return a copy where every review is cleaned and flagged. Flagged text is never shown or acted on."""
    out = {k: v for k, v in details.items() if k != "reviews"}
    out["name"] = clean_text(details.get("name", ""), 60)
    out["reviews"] = []
    for r in details.get("reviews", []):
        raw = str(r.get("text", ""))
        flagged = looks_like_injection(raw)
        out["reviews"].append({
            "rating": r.get("rating"),
            "flagged": flagged,
            "text": "[withheld: contained instructions aimed at an AI agent]" if flagged else clean_text(raw),
        })
    return out
