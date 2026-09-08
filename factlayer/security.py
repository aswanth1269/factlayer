"""Treating uploaded PDFs as untrusted input.

The brief says the system will be tested with documents we have never seen. That
makes every uploaded PDF untrusted input that flows straight into a model
prompt, which is the textbook setup for indirect prompt injection. A PDF can
carry text a human reader never sees: rendered in invisible mode, at zero alpha,
in white on a white page, or at a font size below the threshold of legibility.
A model reading the extracted text layer sees all of it.

Three defences, in order of how much they actually buy:

1. The architecture. Every fact is verified against the span it claims to come
   from before it is stored, so an injected instruction that makes the model
   emit a fabricated number fails verification and is dropped. This is the real
   protection and it was already there for accuracy reasons.
2. Detection. Text a reader cannot see is flagged at ingestion, and any fact
   whose evidence overlaps hidden text is quarantined rather than trusted.
3. Separation. The page is passed to the model as delimited data with an
   explicit instruction that nothing inside it is an instruction.

Nothing here blocks ingestion. A document that trips these checks is still read,
its findings are just quarantined and surfaced, because silently refusing a
legitimate document is a worse failure than flagging one.
"""

import re

# Text below this size is not meaningfully readable at normal zoom.
MIN_READABLE_PT = 4.0
# Channel value above which a colour is treated as white on a white page.
NEAR_WHITE = 240

# Language aimed at a model rather than at a reader. Detection only; a document
# that legitimately discusses prompt injection would trip this too, which is why
# the result is a flag and not a refusal.
INJECTION_PATTERNS = [
    (r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+"
     r"(?:instructions?|prompts?|rules?)", "override instruction"),
    (r"disregard\s+(?:the\s+)?(?:above|previous|prior|system)", "override instruction"),
    (r"\b(?:system|developer)\s*(?:prompt|message|instruction)\b", "prompt reference"),
    (r"you\s+are\s+(?:now\s+)?(?:an?\s+)?(?:AI|assistant|language model|LLM)\b",
     "role reassignment"),
    (r"\bnew\s+(?:instructions?|rules?|task)\s*[:\-]", "instruction injection"),
    (r"do\s+not\s+(?:extract|report|flag|verify|mention)\b", "suppression attempt"),
    (r"(?:mark|treat|report)\s+(?:all\s+)?(?:facts?|figures?|values?)\s+as\b",
     "output steering"),
    (r"\bthis\s+document\s+is\s+authoritative\b", "authority claim"),
    (r"</?(?:system|instruction|prompt)>", "delimiter injection"),
]

_COMPILED = [(re.compile(p, re.I), label) for p, label in INJECTION_PATTERNS]


def _is_white(color_int: int) -> bool:
    r, g, b = (color_int >> 16) & 255, (color_int >> 8) & 255, color_int & 255
    return r >= NEAR_WHITE and g >= NEAR_WHITE and b >= NEAR_WHITE


def hidden_spans(page) -> list[dict]:
    """Return text on the page that a human reader would not see.

    Covers PDF render mode 3 (invisible), zero opacity, near-white fill, and
    sub-legible font sizes.
    """
    found: list[dict] = []

    try:
        for span in page.get_texttrace():
            text = "".join(
                chr(c[0]) for c in span.get("chars", []) if isinstance(c, (list, tuple))
            ).strip()
            if len(text) < 4:
                continue
            reasons = []
            if span.get("type") == 3:
                reasons.append("invisible render mode")
            if span.get("opacity", 1.0) <= 0.05:
                reasons.append("zero opacity")
            if reasons:
                found.append({"text": text, "reasons": reasons,
                              "size": round(span.get("size", 0), 1)})
    except Exception:  # noqa: BLE001 - texttrace is best effort
        pass

    try:
        data = page.get_text("dict")
    except Exception:  # noqa: BLE001
        return found

    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = (span.get("text") or "").strip()
                if len(text) < 4:
                    continue
                reasons = []
                size = span.get("size", 12)
                if size < MIN_READABLE_PT:
                    reasons.append(f"font size {size:.1f}pt")
                if _is_white(span.get("color", 0)):
                    reasons.append("near-white fill")
                if span.get("alpha", 255) == 0:
                    reasons.append("fully transparent")
                if reasons:
                    found.append({"text": text, "reasons": reasons,
                                  "size": round(size, 1)})
    return found


def screen_text(text: str) -> list[dict]:
    """Flag language on the page that is addressed to a model, not a reader."""
    hits = []
    for pattern, label in _COMPILED:
        m = pattern.search(text)
        if m:
            start = max(0, m.start() - 60)
            hits.append({"label": label,
                         "excerpt": text[start : m.end() + 60].replace("\n", " ")})
    return hits


def scan_page(page, text: str) -> list[dict]:
    """Everything worth flagging about one page, as a list of findings."""
    findings = []
    for span in hidden_spans(page):
        findings.append({
            "kind": "hidden text",
            "detail": ", ".join(span["reasons"]),
            "excerpt": span["text"][:300],
        })
    for hit in screen_text(text):
        findings.append({
            "kind": "instruction-like language",
            "detail": hit["label"],
            "excerpt": hit["excerpt"][:300],
        })
    return findings


def evidence_is_hidden(evidence_quote: str, findings: list[dict]) -> str | None:
    """Return a reason if this fact's evidence came from text a reader cannot see."""
    if not evidence_quote:
        return None
    flat = re.sub(r"\s+", "", evidence_quote).lower()
    for f in findings:
        if f["kind"] != "hidden text":
            continue
        hidden = re.sub(r"\s+", "", f["excerpt"]).lower()
        if len(hidden) >= 8 and (hidden in flat or flat in hidden):
            return f"evidence came from hidden text ({f['detail']})"
    return None


# --------------------------------------------------------------------------
# Spreadsheet formula injection
# --------------------------------------------------------------------------
# Document text ends up in exported cells. A spreadsheet treats a string that
# starts with =, +, -, or @ as a formula, so a line of text inside an uploaded
# PDF can become executable content in a workbook someone else opens. Prefixing
# with an apostrophe forces the cell to stay text and the apostrophe is not
# displayed.

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")


def safe_cell(value):
    """Neutralise formula injection before untrusted text reaches a cell."""
    if not isinstance(value, str):
        return value
    if value[:1] in _FORMULA_START:
        return "'" + value
    return value
