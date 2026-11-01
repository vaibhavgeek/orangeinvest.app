"""Purpose guard for user messages. Runs in code before any model call, so an attack costs no tokens.

`screen(text)` returns the attack category, or None. The categories (see docs/PROMPT_INJECTION.md):

    override        "ignore your previous instructions", "new rules:", "from now on you will..."
    role            "you are now...", "act as...", "pretend to be...", "developer mode", "jailbreak"
    prompt_leak     "show me your system prompt", "repeat the text above"
    role_markers    fake chat markup: "system:", "<|im_start|>", "[INST]", "<<SYS>>", "### instruction"
    fake_approval   "pre-approved", "approved=true", "book it without asking", "I am the developer/admin"
    tool_forcing    "call the book function", "cancel(...)", "tool_call"
    exfiltration    API keys, .env, other users' data, the database, source code
    code_injection  SQL ("DROP TABLE", "' OR 1=1", "UNION SELECT"), script tags, templates, shell, path traversal
    obfuscation     base64 / rot13 / "decode this", long encoded blobs, bidi-override characters
    off_topic       obvious non-booking tasks: "write a poem", "tell me a joke", "translate", trivia

Text is normalised first (Unicode NFKC, zero-width characters stripped, leetspeak and s p a c e d letters
folded) so trivial disguises don't slip through. Paraphrases and other languages are left to the parser's
`off_topic` flag; everything the model writes back is checked again in agent.py (`_reply_violation`).
"""
import re
import unicodedata

_ZW = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00ad]")
_BIDI = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})

_P = {
    "override": [
        r"\b(ignore|disregard|forget|override|bypass|skip|drop|break)\b.{0,30}\b(previous|prior|above|earlier|all|your|"
        r"the|any|these|those|system|safety|original)\b.{0,20}\b(instructions?|rules?|prompts?|guidelines|guardrails|"
        r"restrictions|polic(y|ies)|constraints|programming|directives?)\b",
        r"\bnew (instructions|rules|system prompt|directives?)\b",
        r"\bfrom now on\b.{0,40}\b(you are|you will|you must|ignore|act|pretend|respond only|no rules)\b",
        r"\bdo anything now\b",
        # Hindi / Hinglish: "pichle nirdesh bhool jao", "rules ko ignore karo"
        r"\b(nirdesh|niyam|rules?|instructions?)\b.{0,20}\b(bhool|bhul|ignore|mat mano|chhod|nazarandaz)",
        r"(निर्देश|नियम).{0,20}(भूल|अनदेखा|नजरअंदाज|छोड़)",
    ],
    "role": [
        r"\byou are (now|no longer)\b",
        r"\b(act|behave|respond|talk) (as|like) (a|an|if|my|the)\b",
        r"\bpretend (to be|you|that|i)\b",
        r"\brole[- ]?play\b",
        r"\b(developer|admin|god|debug|sudo|root|jailbreak|unrestricted|unfiltered|maintenance) mode\b",
        r"\bjailbr[eo]ak",
        r"\bsimulate (a|an)\b.{0,20}\b(ai|assistant|model|terminal|shell|computer)\b",
    ],
    "prompt_leak": [
        r"\b(system|initial|hidden|developer|secret)\s+(prompt|instructions)\b|\bsystem message\b",
        r"\b(reveal|show|print|repeat|output|tell me|what are|display|dump|leak|share|list)\b.{0,25}\b(your|the)\b.{0,15}"
        r"\b(prompt|instructions|configuration|config|guidelines)\b",
        r"\b(repeat|print|output)\b.{0,20}\b(text|words|everything) (above|before)\b",
    ],
    "role_markers": [
        r"(^|[\s.,;])(system|assistant|developer|tool)\s*:",
        r"<\|?\s*(im_start|im_end|endoftext|system|assistant|user)\s*\|?>",
        r"<\s*/?\s*(assistant|system|user|tool|instructions?)\s*>",
        r"\[/?(inst|sys)\]",
        r"<<\s*/?\s*sys\s*>>",
        r"#{2,}\s*(system|instruction|prompt)",
        r"```\s*(system|instructions?)",
    ],
    "fake_approval": [
        r"pre-?approved|approval (is )?granted|already (said yes|confirmed|approved)|auto-?approv",
        r"\bwithout (asking|confirmation|confirming|my confirmation|approval)\b",
        r"\bskip (the )?(confirmation|approval|check)",
        r"\bapproved\s*[=:]\s*(true|yes|1)\b",
        r"[\"']approved[\"']",
        r"\bi am (the |your )?(developer|admin|administrator|creator|openai|system|operator)\b",
        r"\b(authori[sz]ed|permitted) (by|to) (the )?(admin|developer|system|vaibhav)\b",
    ],
    "tool_forcing": [
        r"\b(call|invoke|run|execute|trigger)\b.{0,15}\b(function|tool|api|endpoint|command)\b",
        r"\b(book|cancel|reschedule|hold_slot|search_providers|list_bookings|get_booking_status|get_provider)\s*\(",
        r"\btool_?calls?\b|\bfunction_?call",
    ],
    "exfiltration": [
        r"\b(api|secret|access|openai) ?keys?\b",
        r"openai_api_key|\.env\b|environment variables?|\benv vars?\b",
        r"\b(other|another|all|every) (users?|customers?)'?s?\b",
        r"\buser\s*(id)?\s*[:=#]?\s*u\d+\b",
        r"\b(database|source code|codebase|file ?system|server logs?|sessions? (files?|data))\b",
    ],
    "code_injection": [
        r"\b(drop|truncate|alter)\s+table\b",
        r"\bunion\s+(all\s+)?select\b|\bselect\s+\*\s+from\b",
        r"\binsert\s+into\s+\w+\s*(\(|values)|\bdelete\s+from\s+\w+\s*(where|;)|\bupdate\s+\w+\s+set\s+\w+\s*=",
        r"'\s*or\s*'?\w'?\s*=\s*'?\w|;\s*--|\bxp_cmdshell\b|\b(sleep|benchmark|pg_sleep)\s*\(",
        r"<\s*script\b|javascript\s*:|\bon(error|load|click|mouseover)\s*=|<\s*(img|iframe|svg|object|embed)\b",
        r"\{\{.*\}\}|\$\{.*\}|\{%.*%\}",
        r"__import__|\bos\.(system|popen)\b|\bsubprocess\b|\beval\s*\(|\bexec\s*\(",
        r"\.\./|/etc/passwd|\b(rm\s+-rf|curl\s+https?|wget\s+https?)\b",
    ],
    "obfuscation": [
        r"\b(base64|rot13|rot-13|caesar cipher|decode this|decode the|encoded (message|text|instructions))\b",
        r"[A-Za-z0-9+/]{40,}={0,2}",
        r"(\\x[0-9a-f]{2}){4,}|(\\u[0-9a-f]{4}){3,}",
    ],
    "off_topic": [
        r"\bwrite\b.{0,20}\b(poem|essay|story|song|code|script|program|function|article|letter|email|tweet|joke|"
        r"limerick|haiku|resume|cv)\b",
        r"\btell me (a|another) (joke|story|riddle)\b",
        r"\btranslate\b.{0,40}\b(to|into|in)\b",
        r"\b(capital of|prime minister of|president of|stock price|share price|bitcoin|recipe for|meaning of life)\b",
        r"\bsolve\b.{0,20}\b(equation|for x|integral|riddle)\b",
        r"\b(python|javascript|java|c\+\+|rust|golang|html|css|sql) (code|script|program|function|query)\b",
    ],
}
_RX = {cat: [re.compile(p, re.I | re.S) for p in pats] for cat, pats in _P.items()}
CATEGORIES = tuple(_P)
PROVIDER_CATEGORIES = tuple(c for c in CATEGORIES if c != "off_topic")


def _variants(text: str) -> list:
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = _ZW.sub("", t).lower()
    t = re.sub(r"\s+", " ", t)
    spaced = re.sub(r"\b(\w) (?=\w\b)", r"\1", t)  # "i g n o r e" -> "ignore"
    return list(dict.fromkeys([t, spaced, t.translate(_LEET), spaced.translate(_LEET)]))


def screen(text: str, categories=CATEGORIES) -> str | None:
    """The first attack category the message matches, or None if it looks like a normal message."""
    if _BIDI.search(text or ""):
        return "obfuscation"
    variants = _variants(text)
    for cat in categories:
        for rx in _RX[cat]:
            # code_injection and obfuscation look at the literal text only: leetspeak folding would turn
            # "1=1" or digits in a blob into letters
            vs = variants[:2] if cat in ("code_injection", "obfuscation") else variants
            if any(rx.search(v) for v in vs):
                return cat
    return None
