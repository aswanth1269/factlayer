"""Guardrails around the model and around the ingest boundary.

Two of these close real holes. The extractor previously trusted whatever JSON
came back and wrote it to the database, and the upload route previously accepted
a file of any size and read every page of it, which is an unbounded bill and an
unbounded disk write triggered by anyone who can reach the endpoint.

The rest are containment: bounded spend per document, bounded retries, and
detection of personal data in spans that are about to be exported.

Everything here fails loudly and locally. A guardrail that silently drops work is
worse than no guardrail, because you stop being able to tell a quiet system from
a broken one.
"""

import os
import random
import re
import threading
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# --------------------------------------------------------------------------
class GuardrailError(Exception):
    """Raised when input or budget limits are exceeded. Surfaced to the caller."""


def env_int(name: str, default: int | None = None) -> int | None:
    """Read an integer setting, treating a blank value as unset.

    A blank line in .env loads as an empty string rather than being absent, so
    int(os.environ[...]) throws on a template that has been copied but not
    filled in. That is a bad first run for a setting that is optional anyway.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise GuardrailError(
            f"{name} must be a whole number, got {raw!r}.") from None


# --------------------------------------------------------------------------
# 1. Output contract
# --------------------------------------------------------------------------
# The model returns free-form JSON. Nothing reaches storage until it fits this
# shape, so a malformed or hallucinated field is a rejected fact rather than a
# corrupt row.

MAX_FACTS_PER_PAGE = 40


class ExtractedFact(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    fact_kind: Literal["measurement", "state"] = "measurement"
    entity: str = Field(min_length=1, max_length=300)
    metric: str = Field(min_length=1, max_length=300)
    value_text: str = Field(default="", max_length=60)
    unit: str = Field(default="", max_length=80)
    state_value: str | None = Field(default=None, max_length=500)
    period: str | None = Field(default=None, max_length=120)
    basis: str | None = Field(default=None, max_length=80)
    scope: str | None = Field(default=None, max_length=200)
    qualifiers: dict[str, str] = Field(default_factory=dict)
    evidence_quote: str = Field(min_length=8, max_length=600)

    @field_validator("qualifiers", mode="before")
    @classmethod
    def _coerce_qualifiers(cls, v: Any) -> dict[str, str]:
        if not isinstance(v, dict):
            return {}
        return {str(k)[:60]: str(val)[:200] for k, val in list(v.items())[:10]}

    @field_validator("value_text")
    @classmethod
    def _strip_footnote_marker(cls, v: str) -> str:
        # "12.7%(2)" is a value with a footnote index glued to it.
        return re.sub(r"\((\d{1,2})\)\s*$", "", v).strip()

    @field_validator("state_value", "period", "basis", "scope", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v


def validate_facts(raw: Any) -> tuple[list[dict], list[str]]:
    """Return (valid facts, reasons the rest were dropped)."""
    if not isinstance(raw, list):
        return [], ["model returned something other than a list of facts"]

    valid: list[dict] = []
    problems: list[str] = []

    if len(raw) > MAX_FACTS_PER_PAGE:
        problems.append(
            f"page produced {len(raw)} facts, capped at {MAX_FACTS_PER_PAGE}")
        raw = raw[:MAX_FACTS_PER_PAGE]

    for item in raw:
        if not isinstance(item, dict):
            problems.append("fact was not an object")
            continue
        try:
            fact = ExtractedFact.model_validate(item)
        except ValidationError as exc:
            first = exc.errors()[0]
            problems.append(
                f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}")
            continue
        if fact.fact_kind == "measurement" and not fact.value_text:
            problems.append("measurement with no value")
            continue
        if fact.fact_kind == "state" and not fact.state_value:
            problems.append("state fact with nothing asserted")
            continue
        valid.append(fact.model_dump())

    return valid, problems


# --------------------------------------------------------------------------
# 2. Ingest limits
# --------------------------------------------------------------------------

MAX_UPLOAD_BYTES = env_int("FACTLAYER_MAX_UPLOAD_MB", 60) * 1024 * 1024
MAX_PAGES_HARD = env_int("FACTLAYER_MAX_PAGES_HARD", 400)


def check_pdf_bytes(head: bytes, size: int) -> None:
    if size == 0:
        raise GuardrailError("The uploaded file is empty.")
    if size > MAX_UPLOAD_BYTES:
        raise GuardrailError(
            f"File is {size / 1e6:.0f} MB, above the "
            f"{MAX_UPLOAD_BYTES / 1e6:.0f} MB limit. Split it or raise "
            "FACTLAYER_MAX_UPLOAD_MB.")
    if not head.startswith(b"%PDF-"):
        raise GuardrailError("That file is not a PDF, whatever it is named.")


def check_document(doc) -> None:
    """Checks that need the PDF open."""
    if getattr(doc, "is_encrypted", False) and doc.needs_pass:
        raise GuardrailError("The PDF is password protected.")
    if doc.page_count > MAX_PAGES_HARD:
        raise GuardrailError(
            f"The PDF has {doc.page_count} pages, above the hard limit of "
            f"{MAX_PAGES_HARD}. Use --max-pages to read a subset.")


# --------------------------------------------------------------------------
# 3. Spend budget
# --------------------------------------------------------------------------
# One upload should never be able to trigger an unbounded number of model calls.

class Budget:
    """A per-document ceiling on model calls, enforced before each call."""

    def __init__(self, max_calls: int | None = None):
        self.max_calls = max_calls or env_int("FACTLAYER_MAX_CALLS_PER_DOC", 150)
        self.used = 0
        self.blocked = 0

    def take(self) -> bool:
        if self.used >= self.max_calls:
            self.blocked += 1
            return False
        self.used += 1
        return True

    def report(self) -> dict:
        return {"model_calls": self.used, "calls_skipped_over_budget": self.blocked,
                "budget": self.max_calls}


# --------------------------------------------------------------------------
# 4. Retries
# --------------------------------------------------------------------------

TRANSIENT = ("rate limit", "429", "timeout", "timed out", "overloaded",
             "503", "502", "connection", "temporarily")


def with_retry(fn, attempts: int | None = None, base: float = 2.0):
    """Retry transient API failures. A bad request is not retried.

    Free tiers answer a burst with 429 rather than a queue, so the retry has to
    be patient enough to outlast the meter's window. Three tries a second and a
    half apart is not, which is why extraction used to lose whole pages the
    moment the worker pool grew. The backoff below reaches roughly a minute of
    total waiting, which is the window most per-minute limits reset on.
    """
    attempts = attempts or env_int("FACTLAYER_RETRIES", 6)
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            message = str(exc).lower()
            if not any(t in message for t in TRANSIENT) or i == attempts - 1:
                raise
            # A 429 means the meter is full, so every worker that hits one has
            # to back off, not just this call. Widening the shared gate here is
            # what stops a pool of 12 from spending its whole run in retries.
            if "429" in message or "rate limit" in message:
                LIMITER.penalise()
            time.sleep(base ** i + random.uniform(0, 0.4))
    raise last  # pragma: no cover


# --------------------------------------------------------------------------
# 4b. Rate limiting
# --------------------------------------------------------------------------
# The worker pool decides how many pages are read at once. The provider decides
# how many calls per minute it will accept. Those are different numbers, and
# without something in between, raising the first one just converts throughput
# into 429s. This is the something in between: one shared gate, so concurrency
# stays a tuning knob for latency rather than a way to trip the meter.


class RateLimiter:
    """A shared minimum spacing between model calls, in requests per minute.

    Set FACTLAYER_RPM to the provider's published limit. Unset means no gate,
    which is the right default for a paid key where concurrency is the point.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0.0
        self._penalty = 0.0

    @property
    def interval(self) -> float:
        rpm = env_int("FACTLAYER_RPM", 0) or 0
        return 60.0 / rpm if rpm > 0 else 0.0

    def acquire(self) -> None:
        """Block until this caller's turn. A no-op when FACTLAYER_RPM is unset."""
        gap = self.interval
        if gap <= 0 and self._penalty <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + gap + self._penalty
            # Each successful pass repays a little of the penalty, so one burst
            # of 429s does not slow the rest of the run forever.
            self._penalty = max(0.0, self._penalty - 0.25)
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def penalise(self, seconds: float = 2.0) -> None:
        """Widen the gate after a 429, up to a ceiling."""
        with self._lock:
            self._penalty = min(10.0, self._penalty + seconds)


LIMITER = RateLimiter()


# --------------------------------------------------------------------------
# 5. Personal data
# --------------------------------------------------------------------------
# Filings name people. Evidence spans about directors and officers carry
# contact details, identifiers and addresses, and those spans end up in an
# exported workbook that gets emailed around.
#
# Detection is always on. Masking is off by default, because a system that
# quietly rewrites its own evidence is no longer showing you the source. Turn it
# on with FACTLAYER_MASK_PII=1 when exporting something you intend to share.

PII_PATTERNS = [
    ("email", r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"),
    ("phone", r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)"),
    ("PAN", r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    ("DIN", r"\bDIN[:\s]*\d{8}\b"),
    ("national ID", r"(?<!\d)\d{4}\s\d{4}\s\d{4}(?!\d)"),
]

_PII = [(label, re.compile(p)) for label, p in PII_PATTERNS]

MASK_PII = (os.environ.get("FACTLAYER_MASK_PII") or "").strip().lower() in {"1", "true", "yes"}


def find_pii(text: str) -> list[str]:
    if not text:
        return []
    return sorted({label for label, pattern in _PII if pattern.search(text)})


def mask_pii(text: str, force: bool | None = None) -> str:
    if not text or not (MASK_PII if force is None else force):
        return text
    for label, pattern in _PII:
        text = pattern.sub(f"[{label} redacted]", text)
    return text
