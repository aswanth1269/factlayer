"""One tiny call surface for the model, so the provider is a one line swap.

The model is used for exactly two jobs: reading a page into structured facts,
and clustering metric surface forms into canonical keys. Every comparison,
conversion and contradiction decision downstream is plain Python, which is why
the reasoning shown in the UI can be trusted and explained line by line.
"""

import json
import os
import re

from .guardrails import with_retry

PROVIDER = (os.environ.get("FACTLAYER_PROVIDER") or "").strip().lower()
OPENAI_MODEL = os.environ.get("FACTLAYER_OPENAI_MODEL") or "gpt-4o-mini"
# Any OpenAI-compatible endpoint works here. Google AI Studio and Groq both
# expose one, which is how this runs on a free tier without changing any code.
OPENAI_BASE_URL = os.environ.get("FACTLAYER_OPENAI_BASE_URL") or None
ANTHROPIC_MODEL = os.environ.get("FACTLAYER_ANTHROPIC_MODEL") or "claude-sonnet-4-6"


def _provider() -> str:
    if PROVIDER:
        return PROVIDER
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    raise RuntimeError(
        "Set OPENAI_API_KEY or ANTHROPIC_API_KEY (see .env.example)."
    )


def complete_json(system: str, user: str, max_tokens: int = 4000) -> dict:
    """Ask for a JSON object and return it parsed. Raises on unparseable output."""
    return with_retry(lambda: _complete_json(system, user, max_tokens))


def _complete_json(system: str, user: str, max_tokens: int) -> dict:
    p = _provider()
    if p == "openai":
        from openai import OpenAI

        client = OpenAI(base_url=OPENAI_BASE_URL) if OPENAI_BASE_URL else OpenAI()
        r = client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=max_tokens,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        raw = r.choices[0].message.content
    else:
        import anthropic

        client = anthropic.Anthropic()
        r = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user + "\n\nReturn a JSON object only."}],
        )
        raw = "".join(b.text for b in r.content if b.type == "text")

    return _parse(raw)


def _parse(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            return json.loads(raw[start : end + 1])
        raise
