"""A deterministic extractor, for when there is no model to call.

PyMuPDF reads 227 pages of the starter set in about nine seconds. The model
that turns those pages into structured facts takes roughly two minutes per
page, and on a free tier it stops answering altogether. That is a bad thing to
have as a single point of failure in a system whose whole claim is that its
reasoning is inspectable.

So this reads the same pages with regular expressions instead. It is worse than
the model at deciding what a fact is about: it will call the label next to a
number the metric, and it has no idea what the number means. It is better in
every other respect a demo cares about, because it costs nothing, runs offline,
and finishes a hundred page document in under a second.

The important part is that it emits exactly the shape the model emits, so it
lands in the same output contract, the same span verification, the same
coordinate model and the same relation engine. Nothing downstream can tell the
two apart, which is what makes the extractor a swappable part rather than the
system's foundation.

Nothing here knows anything about logistics, finance or India. The patterns are
about how numbers are printed, and the entity and period vocabularies are
learned from the document being read.
"""

import re
from collections import Counter

# --------------------------------------------------------------------------
# What a printed number looks like
# --------------------------------------------------------------------------
# Both grouping conventions, because a document written in India groups as
# 1,23,456 and one written elsewhere groups as 123,456, and a corpus can hold
# both at once.

_NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"

_CURRENCY = r"(?:₹|Rs\.?|INR|US\$|USD|\$|€|EUR|£|GBP)"

# Magnitude and unit words that follow a number. Ordered longest first so that
# "million tons" wins over "million".
_UNIT = (
    r"(?:%|per\s?cent|percent|bps|"
    r"(?:mn|bn|tn|k|cr|lakhs?|crores?|millions?|billions?|trillions?|thousands?)"
    r"(?:\s+(?:tons?|tonnes?|sq\.?\s?ft\.?|sq\.?\s?m\.?|units?|customers?|"
    r"shipments?|orders?|users?|people|employees?))?|"
    r"tons?|tonnes?|kg|kms?|sq\.?\s?ft\.?|sq\.?\s?m\.?|acres?|"
    r"pin\s?codes?|days?|hours?|x|times)"
)

# A value as printed: optional currency, the digits, optional unit, with
# brackets meaning negative kept intact because the coordinate model reads them.
VALUE_RE = re.compile(
    rf"(?P<value>\(?\s*(?:{_CURRENCY})?\s*(?:{_NUM})\s*\)?)"
    rf"(?P<unit>\s*(?:{_UNIT}))?",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# Coordinates that can be read off the page without understanding it
# --------------------------------------------------------------------------

PERIOD_RES = [
    re.compile(r"\b(Q[1-4]\s?[’']?\s?FY\s?\d{2,4})\b", re.I),
    re.compile(r"\b(H[12]\s?[’']?\s?FY\s?\d{2,4})\b", re.I),
    re.compile(r"\b(FY\s?[’']?\s?\d{2,4})\b", re.I),
    re.compile(r"\b(CY\s?\d{4})\b", re.I),
    re.compile(r"\b(as\s+(?:of|on|at)\s+\d{1,2}\s+\w+\s+\d{4})\b", re.I),
    re.compile(r"\b(as\s+(?:of|on|at)\s+\w+\s+\d{1,2},?\s+\d{4})\b", re.I),
    re.compile(r"\b(\d{4}\s?[-–]\s?\d{2,4})\b"),
    re.compile(r"\b((?:January|February|March|April|May|June|July|August|"
               r"September|October|November|December)\s+\d{1,2},?\s+\d{4})\b", re.I),
]

BASIS_RE = re.compile(
    r"\b(adjusted|pro\s?forma|restated|provisional|projected|reported|"
    r"like[-\s]?for[-\s]?like|normalis(?:ed|ing)|normaliz(?:ed|ing))\b", re.I)

SCOPE_RE = re.compile(r"\b(consolidated|standalone|segment[-\s]?wise)\b", re.I)

# Lines that are page furniture rather than content.
FURNITURE_RE = re.compile(
    r"^\s*(?:page\s*\d+|\d+\s*(?:of|/)\s*\d+|[ivxlc]+|\d{1,3})\s*$", re.I)

# A corporate name looks like a Titlecase run ending in one of these.
ORG_SUFFIX = (r"(?:Limited|Ltd\.?|Inc\.?|LLP|PLC|Corporation|Corp\.?|Company|"
              r"Bank|Authority|Ministry|Department|Fund|Board|Council|"
              r"Commission|Institute|Organisation|Organization)")
# At least one Titlecase word before the suffix, otherwise the most common
# "organisation" in any filing is the bare word Company.
ORG_RE = re.compile(rf"\b((?:[A-Z][\w&.\-]*\s+){{1,5}}{ORG_SUFFIX})\b")

# An acronym or Titlecase prefix in front of a lowercase word, which is how a
# deck writes "PTL freight tonnage" or "Express Parcel service revenue".
SUBJECT_RE = re.compile(r"^((?:[A-Z]{2,}|[A-Z][a-z]+)(?:\s+(?:[A-Z]{2,}|[A-Z][a-z]+)){0,2})\s+(?=[a-z])")

STOP_LABEL = re.compile(r"^(?:and|or|the|of|in|to|for|a|an|as|at|by|on|from|"
                        r"with|vs\.?|versus|total|note[s]?)$", re.I)

# Identifiers and dates are printed like quantities but are not measurements.
# Comparing two of them would be meaningless, so they never become facts.
IDENTIFIER_LABEL_RE = re.compile(
    r"\b(?:date|dated|scrip|isin|cin|code|no\.?|number|ref(?:erence)?|"
    r"tel(?:ephone)?|phone|fax|pin|gst(?:in)?|pan|regd?\.?|registration|"
    r"folio|page|clause|section|annexure|website|email|e-mail)\b", re.I)

# A bare four digit year, or something already shaped like a date.
YEARLIKE_RE = re.compile(r"^\(?\s*(?:19|20)\d{2}\s*\)?$")
DATELIKE_RE = re.compile(r"\d{1,4}[./-]\d{1,2}(?:[./-]\d{1,4})?$")


# Words that only ever appear mid-sentence. A label carrying one of these is a
# fragment of running prose that happens to sit next to a number, not the name
# of a metric, and admitting it produces keys like "grew_by_fy24_fastest" that
# can never match anything in another document.
PROSE_RE = re.compile(
    r"\b(?:grew|grow|growing|rose|rising|fell|falling|increased|increasing|"
    r"decreased|declining|declined|took|takes|went|going|driven|led|leading|"
    r"compared|versus|reflects?|reflecting|which|that|this|these|those|"
    r"we|our|us|they|their|it|its|has|have|had|was|were|been|being|"
    r"is|are|will|would|could|should|may|might|during|while|whereas|"
    r"including|includes?|excluding|excludes?|approximately|roughly|about)\b",
    re.I)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip(" \t:•·–—-|,")


def _strip_periods(s: str) -> str:
    """Remove period expressions from a metric name.

    The period is one of the coordinates a fact is compared on, so it must not
    also be part of the metric's identity. Leaving it in means "FY24 revenue
    from services" and "Revenue from services" are two different metrics that
    can never corroborate each other, which defeats the entire point of having
    a period axis in the first place.
    """
    out = s
    for rx in PERIOD_RES:
        out = rx.sub(" ", out)
    # The connectives that were holding the period in place go with it.
    out = re.sub(r"\b(?:in|for|as of|as on|as at|during|ended|ending)\s*$", " ", out, flags=re.I)
    out = re.sub(r"^\s*(?:in|for|as of|as on|as at|during|ended|ending)\b", " ", out, flags=re.I)
    return _clean(out)


def _is_labelish(s: str) -> bool:
    """A label needs letters, some length, and must not be page furniture."""
    s = _clean(s)
    if len(s) < 3 or len(s) > 160:
        return False
    if FURNITURE_RE.match(s):
        return False
    letters = sum(c.isalpha() for c in s)
    if letters < 3 or letters / len(s) < 0.35:
        return False
    return not STOP_LABEL.match(s)


def _find_period(*texts: str) -> str | None:
    for text in texts:
        if not text:
            continue
        for rx in PERIOD_RES:
            m = rx.search(text)
            if m:
                return _clean(m.group(1))
    return None


def page_period(text: str) -> str | None:
    """The period this page is mostly about.

    A chart's axis labels name every year on the page, so taking the first
    period found in the header stamps a slide about FY24 with whichever year
    the axis happens to start at. The period that appears most often is a far
    better guess, and ties break toward the more specific label, since a page
    that says both "FY24" and "Q4 FY24" is reporting the quarter.
    """
    counts: Counter[str] = Counter()
    for rx in PERIOD_RES:
        for m in rx.finditer(text or ""):
            counts[_clean(m.group(1))] += 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]


def _first(rx: re.Pattern, *texts: str) -> str | None:
    for text in texts:
        if text:
            m = rx.search(text)
            if m:
                return m.group(1).lower()
    return None


def document_entity(page_texts: list[str]) -> str | None:
    """Guess what the document is about, from the document itself.

    The most repeated organisation-shaped name across the pages read. This is
    the fallback subject for a number whose own label names no subject, which
    on a filing or a results deck is nearly always the issuer.
    """
    counts: Counter[str] = Counter()
    for text in page_texts:
        for m in ORG_RE.finditer(text or ""):
            counts[_clean(m.group(1))] += 1
    if not counts:
        return None
    # Prefer the longest form among the near-tied leaders, so "Delhivery
    # Limited" beats a bare "Limited" that appears just as often.
    top = counts.most_common(8)
    best = max((c for c in top if c[1] >= top[0][1] * 0.6),
               key=lambda c: (c[1], len(c[0])))
    return best[0]


def _split_subject(label: str, fallback: str | None) -> tuple[str, str]:
    """Split a label into (entity, metric).

    "PTL freight tonnage" is a metric about PTL. "Revenue from services" names
    no subject, so the subject is whatever the document is about.
    """
    label = _clean(label)
    m = SUBJECT_RE.match(label)
    if m:
        subject, rest = m.group(1), label[m.end():].strip()
        if len(rest) >= 3:
            return subject, rest
    return (fallback or "document"), label


def _labels_near(lines: list[str], i: int, without_value: str) -> list[str]:
    """Candidate labels for a value on line i, nearest first.

    A table writes "Revenue from services 7,224" on one line. A slide writes
    the number large with its caption on the line below. Both are common, so
    the remainder of the line is tried first and the neighbours after it.
    """
    out = [without_value]
    for j in (i + 1, i - 1, i + 2):
        if 0 <= j < len(lines):
            out.append(lines[j])
    return [c for c in (_clean(x) for x in out) if _is_labelish(c)]


MAX_PER_PAGE = 40


def extract_page(text: str, subject: str | None = None) -> list[dict]:
    """Read one page into facts, with no model and no network.

    Emits the same dicts the model emits, so the caller cannot tell which
    extractor produced them and neither can anything downstream.
    """
    lines = [ln for ln in (text or "").splitlines()]
    fallback_period = page_period(text)
    facts: list[dict] = []
    seen: set[tuple] = set()

    for i, line in enumerate(lines):
        stripped = _clean(line)
        if not stripped or FURNITURE_RE.match(stripped):
            continue

        for m in VALUE_RE.finditer(line):
            value_text = _clean(m.group("value"))
            unit = _clean(m.group("unit") or "")
            if not re.search(r"\d", value_text):
                continue
            # A bare small integer on its own is far more often a bullet, a
            # year or a table index than a measurement. Require a unit, a
            # separator or a decimal point to treat it as a quantity.
            bare = re.fullmatch(r"\(?\s*\d{1,4}\s*\)?", value_text)
            if bare and not unit:
                continue
            if len(value_text) > 60:
                continue
            # A year or a date is printed like a quantity but is a coordinate,
            # not a measurement. So is anything sitting next to an identifier
            # label: a scrip code and a phone number compare to nothing.
            if not unit and (YEARLIKE_RE.match(value_text)
                             or DATELIKE_RE.search(value_text)):
                continue

            remainder = _clean(line[:m.start()] + " " + line[m.end():])
            candidates = _labels_near(lines, i, remainder)
            if not candidates:
                continue
            label = candidates[0]
            if IDENTIFIER_LABEL_RE.search(label):
                continue

            # Keep the period for reading off the coordinate, then drop it from
            # the name so the metric's identity is the metric alone.
            period = _find_period(label, stripped) or fallback_period
            name = _strip_periods(label)
            if PROSE_RE.search(name) or len(name.split()) > 8:
                continue

            entity, metric = _split_subject(name, subject)
            if len(metric) < 3:
                continue

            # The quote has to contain the value and has to appear on the page
            # verbatim, because verify() checks both. Using the untouched
            # source line guarantees it.
            quote = line.strip()
            if len(quote) < 8:
                quote = _clean(line + " " + (lines[i + 1] if i + 1 < len(lines) else ""))
            if len(quote) < 8 or len(quote) > 600:
                continue

            key = (entity.lower(), metric.lower(), value_text, unit.lower())
            if key in seen:
                continue
            seen.add(key)

            facts.append({
                "fact_kind": "measurement",
                "entity": entity[:300],
                "metric": metric[:300],
                "value_text": value_text[:60],
                "unit": unit[:80],
                "state_value": None,
                "period": period,
                "basis": _first(BASIS_RE, label, stripped, text),
                "scope": _first(SCOPE_RE, label, stripped, text),
                "qualifiers": {"extractor": "rules"},
                "evidence_quote": quote[:600],
            })
            if len(facts) >= MAX_PER_PAGE:
                return facts

    return facts


def extract_all(chunks: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Read every page. No pool, because there is nothing to wait for."""
    subject = document_entity([c.get("text", "") for c in chunks])
    return [(c, extract_page(c.get("text", ""), subject)) for c in chunks]
