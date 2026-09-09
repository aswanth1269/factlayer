"""Find a model that still has quota, using one call per candidate.

Free tiers meter per model, so a model you have not touched today has its own
allowance. This spends one small request on each candidate and reports which
ones answer, which are rate limited, and which do not exist for your key.

    uv run python probe_models.py
    uv run python probe_models.py gemini-3.5-flash gemini-3.7-flash
"""

import os
import sys

import factlayer  # noqa: F401  - loads .env

# Lite and flash tiers first. Extraction here is structured output at low
# creativity, so the cheapest tier that returns valid JSON is the right pick.
CANDIDATES = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-2.5-flash",
    "gemini-flash-latest",
]

PROMPT = 'Reply with exactly this JSON and nothing else: {"ok": true}'


def main() -> int:
    from openai import OpenAI

    key = os.environ.get("OPENAI_API_KEY")
    base = os.environ.get("FACTLAYER_OPENAI_BASE_URL")
    if not key:
        print("No OPENAI_API_KEY found. Check your .env.")
        return 1

    client = OpenAI(api_key=key, base_url=base) if base else OpenAI(api_key=key)
    models = sys.argv[1:] or CANDIDATES
    working = []

    for name in models:
        try:
            r = client.chat.completions.create(
                model=name,
                max_tokens=40,
                temperature=0,
                messages=[{"role": "user", "content": PROMPT}],
            )
            reply = (r.choices[0].message.content or "").strip().replace("\n", " ")
            print(f"  OK        {name}  ->  {reply[:60]}")
            working.append(name)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                limit = ""
                if "PerDay" in msg:
                    limit = " (daily quota spent)"
                elif "PerMinute" in msg:
                    limit = " (per-minute limit, may work if paced)"
                print(f"  QUOTA     {name}{limit}")
            elif "404" in msg or "NOT_FOUND" in msg:
                print(f"  MISSING   {name}")
            else:
                print(f"  ERROR     {name}  ->  {msg[:110]}")

    print()
    if working:
        print("Usable right now:")
        for name in working:
            print(f"  FACTLAYER_OPENAI_MODEL={name}")
        print("\nPut one of those in .env, then run the earnings deck.")
    else:
        print("Nothing has quota left. Switch provider or use a paid key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())