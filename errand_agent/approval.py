"""Approval records. The only way an approval becomes 'granted' is the agent's code seeing an
explicit user confirmation while that exact record is pending. Tool calls check the record."""
import hashlib
import re
import uuid


class ApprovalError(Exception):
    """A mutating tool call was attempted without a matching, granted approval."""


def new_approval(action: str, provider_id: str, provider_name: str, slot: str, price: int,
                 booking_id: str | None = None, now: str = "", approval_id: str | None = None) -> dict:
    aid = approval_id or uuid.uuid4().hex[:10]
    ap = {"id": aid, "action": action, "provider_id": provider_id, "provider_name": provider_name,
          "slot": slot, "price": int(price), "booking_id": booking_id, "status": "pending",
          "idempotency_key": f"{action}-{aid}", "created_at": now}
    ap["fingerprint"] = fingerprint(ap)
    return ap


def fingerprint(ap: dict) -> str:
    raw = "|".join(str(ap.get(k)) for k in ("action", "provider_id", "slot", "price", "booking_id"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def require(ap: dict | None, action: str, provider_id: str | None = None, slot: str | None = None,
            price: int | None = None, booking_id: str | None = None) -> None:
    if not ap:
        raise ApprovalError(f"{action}: no approval record")
    if ap.get("status") != "granted":
        raise ApprovalError(f"{action}: approval is {ap.get('status')}, not granted")
    if ap.get("fingerprint") != fingerprint(ap):
        raise ApprovalError(f"{action}: approval record was modified after creation")
    expected = {"action": action, "provider_id": provider_id, "slot": slot, "price": price, "booking_id": booking_id}
    for k, v in expected.items():
        if v is not None and ap.get(k) != v:
            raise ApprovalError(f"{action}: {k} {v!r} does not match approved {ap.get(k)!r}")


_AFFIRM_START = {"yes", "y", "yep", "yeah", "yup", "confirm", "confirmed", "ok", "okay", "sure",
                 "go", "do", "book", "please", "correct", "approve", "approved"}
_AFFIRM_PHRASES = {"go ahead", "do it", "book it", "please do", "sounds good", "yes please", "confirm it"}
_HEDGES = {"not", "don't", "dont", "no", "wait", "but", "actually", "change", "instead", "hmm", "maybe", "later",
           # instruction-like words: a confirmation that also tries to steer the agent is not a clear yes
           "ignore", "rules", "rule", "system", "instructions", "instruction", "override", "bypass", "skip",
           "previous", "admin", "developer", "prompt", "pretend"}
_NEG_START = {"no", "nope", "nah", "stop", "don't", "dont", "never"}


def _words(text: str) -> list:
    return re.findall(r"[a-z']+", (text or "").lower())


def is_affirmative(text: str) -> bool:
    w = _words(text)
    if not w or len(w) > 6 or any(x in _HEDGES for x in w):
        return False
    joined = " ".join(w)
    if w[0] in {"go", "do", "book", "please"}:
        return any(joined.startswith(p) for p in _AFFIRM_PHRASES)
    return w[0] in _AFFIRM_START or joined in _AFFIRM_PHRASES


def is_negative(text: str) -> bool:
    w = _words(text)
    if not w:
        return False
    joined = " ".join(w)
    return w[0] in _NEG_START or joined in {"cancel that", "cancel it", "not now", "never mind", "do not"}
