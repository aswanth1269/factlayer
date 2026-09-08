"""Turn a PDF into page-level chunks that carry their own provenance.

Chunking is page-level on purpose. Financial and institutional PDFs put most of
their facts inside tables, and a fixed-size character window slices tables in
half. A page is the smallest unit that reliably keeps a table with its header
row, and it also gives every fact a page number a human can check.

Pages are ranked by how fact-dense they look so that a page cap spends the
budget on the pages that matter. The ranking uses only generic signals such as
digit density and currency or percent markers, never document-specific keywords,
because the system has to work on PDFs it has never seen.
"""

import hashlib
import re

import pymupdf

from . import guardrails, security


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


_DIGIT = re.compile(r"\d")
_MONEY = re.compile(r"[₹$€£]|\b(?:crore|cr|lakh|million|mn|billion|bn|trillion)\b", re.I)
_PCT = re.compile(r"%")
_PERIOD = re.compile(r"\b(?:FY\s?\d{2,4}|Q[1-4]|H[12]|20\d{2}[-/]\d{2})\b", re.I)
_STATE = re.compile(
    r"\b(?:appointed|resigned|retired|ceased|effective from|w\.e\.f\.|"
    r"registered office|incorporated|renamed)\b",
    re.I,
)


def page_score(text: str) -> float:
    """Generic fact-density heuristic. No document-specific vocabulary."""
    n = len(text)
    if n < 120:
        return 0.0
    digits = len(_DIGIT.findall(text)) / n
    score = digits * 40
    score += min(len(_MONEY.findall(text)), 12) * 0.6
    score += min(len(_PCT.findall(text)), 12) * 0.4
    score += min(len(_PERIOD.findall(text)), 12) * 0.8
    score += min(len(_STATE.findall(text)), 6) * 1.2
    # A page that is mostly one long paragraph of prose is unlikely to be dense
    # in extractable measurements.
    lines = text.count("\n") + 1
    if lines > 12:
        score += 1.0
    return score


def read_pdf(path: str, max_pages: int | None = None) -> tuple[int, list[dict]]:
    """Return (total_pages, chunks). Chunks come back in page order.

    If max_pages is set, the highest-scoring pages are kept but their original
    page numbers are preserved so evidence always points at the real page.
    """
    doc = pymupdf.open(path)
    guardrails.check_document(doc)
    total = doc.page_count

    pages = []
    for i in range(total):
        page = doc[i]
        text = page.get_text("text")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) < 80:
            continue
        # Uploaded PDFs are untrusted input. Anything a reader cannot see, or
        # any language addressed to a model rather than a reader, is recorded
        # here so facts drawn from it can be quarantined later.
        pages.append({"page": i + 1, "text": text, "score": page_score(text),
                      "findings": security.scan_page(page, text)})
    doc.close()

    if max_pages is not None and len(pages) > max_pages:
        pages = sorted(pages, key=lambda p: -p["score"])[:max_pages]
        pages.sort(key=lambda p: p["page"])

    chunks = []
    for p in pages:
        # Very long pages are split on blank lines so a single request never
        # blows past the model's useful attention span.
        for part in _split_long(p["text"], limit=6000):
            chunks.append({"page": p["page"], "text": part,
                           "sha256": sha256(part), "findings": p["findings"]})
    return total, chunks


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for para in text.split("\n\n"):
        if buf and len(buf) + len(para) + 2 > limit:
            out.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        out.append(buf)
    return out
