"""One tiny call surface for the model, so the provider is a one line swap.

The model is used for exactly two jobs: reading a page into structured facts,
and clustering metric surface forms into canonical keys. Every comparison,
conversion and contradiction decision downstream is plain Python, which is why
the reasoning shown in the UI can be trusted and explained line by line.
"""

import json
import os
import re

from .guardrails import LIMITER, env_int, with_retry

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


def complete_json(system: str, user: str, max_tokens: int | None = None) -> dict:
    """Ask for a JSON object and return it parsed. Raises on unparseable output."""
    ceiling = max_tokens or env_int("FACTLAYER_MAX_TOKENS", 4000)
    return with_retry(lambda: _complete_json(system, user, ceiling))


def _complete_json(system: str, user: str, max_tokens: int) -> dict:
    LIMITER.acquire()
    p = _provider()
    if p == "openai":
        from openai import OpenAI

        # Without a deadline a stalled request holds its worker for the SDK's
        # ten minute default, so one bad call parks a whole slot of the pool.
        # Failing at 90s and retrying is strictly faster than waiting.
        opts = {"timeout": float(env_int("FACTLAYER_TIMEOUT", 90)), "max_retries": 0}
        client = (OpenAI(base_url=OPENAI_BASE_URL, **opts) if OPENAI_BASE_URL
                  else OpenAI(**opts))
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
        choice = r.choices[0]
        raw = choice.message.content
        # Reasoning models spend the same budget on thinking that they spend on
        # the answer, so a page that needs a long chain of thought comes back
        # with finish_reason "length" and content None. Saying so beats a
        # NoneType error three frames down, and it names the fix.
        if not raw:
            reason = getattr(choice, "finish_reason", None)
            if reason == "length":
                raise RuntimeError(
                    f"{OPENAI_MODEL} hit the {max_tokens} token ceiling before "
                    "it finished the JSON. Raise FACTLAYER_MAX_TOKENS, or use a "
                    "model that does not reason before answering."
                )
            raise RuntimeError(
                f"{OPENAI_MODEL} returned an empty message (finish_reason={reason})."
            )
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
