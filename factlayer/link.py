"""Compare facts and explain the result. No model calls in this file.

Two facts are only ever compared once they share an entity and a canonical
metric. Within that block, the decision is a coordinate comparison:

  same coordinates, values agree      -> corroborates
  same coordinates, values disagree   -> contradicts
  coordinates differ                  -> reconciled, and we name the axis

That third branch is the one that matters. In real filings almost every
apparent conflict is a period, basis or scope difference, and a system that
cannot say which axis differs will report a wall of false contradictions.
"""

import itertools
import json
import uuid

from . import db

AXES = ["period", "basis", "scope"]

AXIS_PHRASE = {
    "period": "they cover different periods",
    "basis": "they are stated on a different basis",
    "scope": "they cover a different scope",
}

# Facts inside one block are compared pairwise, so a runaway block would be
# quadratic. Blocks are capped and the cap is reported rather than silently
# applied.
MAX_BLOCK = 60


def _axes_of(f: dict) -> dict:
    return {
        "period": (f.get("period_start"), f.get("period_end")),
        "basis": f.get("basis") or "reported",
        "scope": f.get("scope") or "unspecified",
    }


def _values_agree(a: dict, b: dict) -> tuple[bool, float, float]:
    """Compare in the base unit, allowing for how coarsely each was printed."""
    va, vb = a.get("value_base"), b.get("value_base")
    if va is None or vb is None:
        return (False, 0.0, 0.0)
    gap = abs(va - vb)
    tol = max(a.get("tolerance_base") or 0.0, b.get("tolerance_base") or 0.0)
    # A small relative allowance covers figures that were rounded upstream
    # before ever being printed.
    tol = max(tol, 0.005 * max(abs(va), abs(vb)))
    return (gap <= tol, gap, tol)


def _fmt(f: dict) -> str:
    if f.get("fact_kind") == "state":
        return f"{f.get('state_value')}"
    return f"{f.get('value_text')} {f.get('unit') or ''}".strip()


def _period_label(f: dict) -> str:
    return f.get("period") or "no period stated"


def _find_bridge(a: dict, b: dict) -> dict | None:
    """Look for a third fact whose value equals the gap between two others.

    When revenue from services and total revenue from operations disagree, the
    difference is usually another fact already in the store, such as revenue
    from traded goods. Finding it turns an unexplained numeric gap into an
    arithmetic identity backed by three pieces of evidence.
    """
    va, vb = a.get("value_base"), b.get("value_base")
    if va is None or vb is None:
        return None
    gap = abs(va - vb)
    if gap <= 0:
        return None
    tol = max(0.01 * gap, a.get("tolerance_base") or 0.0, b.get("tolerance_base") or 0.0)

    candidates = db.rows(
        """SELECT * FROM facts
           WHERE verified = 1 AND entity_key = ? AND base_unit = ?
             AND id NOT IN (?, ?)
             AND value_base BETWEEN ? AND ?
             AND (period_start IS ? OR period_start = ?)""",
        (a["entity_key"], a["base_unit"], a["id"], b["id"],
         gap - tol, gap + tol, a.get("period_start"), a.get("period_start")),
    )
    if not candidates:
        return None
    c = candidates[0]
    return {
        "kind": "arithmetic",
        "fact_id": c["id"],
        "metric": c["metric"],
        "value_text": c["value_text"],
        "unit": c["unit"],
        "page": c["page"],
        "doc_id": c["doc_id"],
        "evidence_quote": c["evidence_quote"],
        "statement": (
            f"The gap equals {c['value_text']} {c['unit'] or ''} of "
            f"{c['metric']}, which is itself recorded on page {c['page']}."
        ),
    }


def classify(a: dict, b: dict) -> dict | None:
    ax_a, ax_b = _axes_of(a), _axes_of(b)
    differ = [k for k in AXES if ax_a[k] != ax_b[k]]

    # State facts: same axes means they should assert the same thing.
    if a.get("fact_kind") == "state" or b.get("fact_kind") == "state":
        sa = (a.get("state_value") or "").strip().lower()
        sb = (b.get("state_value") or "").strip().lower()
        if not sa or not sb:
            return None
        same = sa == sb or sa in sb or sb in sa
        if same and not differ:
            return {"rel_type": "corroborates",
                    "explanation": "Both statements assert the same thing about "
                                   f"{a['entity']} with matching context."}
        if same:
            return None
        if "period" in differ:
            return {
                "rel_type": "reconciled",
                "axes": ["period"],
                "explanation": (
                    f"These describe {a['entity']} at different points in time "
                    f"({_period_label(a)} versus {_period_label(b)}), so this is a "
                    "change of state rather than a conflict."),
            }
        if not differ:
            return {"rel_type": "contradicts",
                    "explanation": "Both statements describe the same subject in the "
                                   "same context but assert different things."}
        return None

    if a.get("dimension") != b.get("dimension"):
        return None

    agree, gap, tol = _values_agree(a, b)
    unit_differs = (a.get("unit") or "").lower() != (b.get("unit") or "").lower()

    if not differ:
        if agree:
            note = ""
            if unit_differs:
                note = (f" The figures are written differently "
                        f"({_fmt(a)} and {_fmt(b)}) but resolve to the same "
                        f"quantity once converted to {a['base_unit']}.")
            return {
                "rel_type": "corroborates",
                "explanation": (
                    f"Both sources report {a['metric']} for {_period_label(a)} on "
                    f"the same basis and scope, and the values agree within the "
                    f"rounding implied by how they are printed.{note}"),
            }
        rel = {
            "rel_type": "contradicts",
            "explanation": (
                f"Both sources report {a['metric']} for {_period_label(a)} with "
                f"identical period, basis and scope, yet the values differ by "
                f"{gap:,.0f} {a['base_unit']}, which is beyond the "
                f"{tol:,.0f} tolerance implied by their own precision. No context "
                f"axis explains the gap."),
        }
        bridge = _find_bridge(a, b)
        if bridge:
            rel["rel_type"] = "reconciled"
            rel["axes"] = ["derived"]
            rel["support"] = bridge
            rel["explanation"] = (
                f"These two figures for {a['metric']} differ, but the difference is "
                f"accounted for by another fact in the corpus. {bridge['statement']}")
        return rel

    if agree:
        return {
            "rel_type": "related",
            "axes": differ,
            "explanation": (
                f"Same value for {a['metric']} but {AXIS_PHRASE[differ[0]]}, so "
                "these are separate facts that happen to coincide."),
        }

    reasons = " and ".join(AXIS_PHRASE[k] for k in differ)
    detail = []
    if "period" in differ:
        detail.append(f"{_period_label(a)} versus {_period_label(b)}")
    if "basis" in differ:
        detail.append(f"{a.get('basis')} versus {b.get('basis')}")
    if "scope" in differ:
        detail.append(f"{a.get('scope')} versus {b.get('scope')}")

    return {
        "rel_type": "reconciled",
        "axes": differ,
        "explanation": (
            f"{_fmt(a)} and {_fmt(b)} look like a conflict over {a['metric']}, but "
            f"{reasons}: {'; '.join(detail)}. Once that is taken into account the "
            "two figures are consistent."),
    }


def rebuild_entities(entity_keys: list[str]) -> dict:
    """Re-link only the entities a new document touched.

    A full rebuild compares every fact against every other fact sharing its
    entity and metric, so its cost grows with the size of the whole corpus. An
    upload only ever changes the blocks it adds facts to, so this walks those
    blocks and leaves the rest of the layer alone. The result is identical to a
    full rebuild, because classify() is a pure function of the pair it is given.
    """
    if not entity_keys:
        return _counts(0, 0, 0)
    totals = {"relations": 0, "blocks": 0, "blocks_truncated": 0}
    for key in entity_keys:
        s = rebuild(key)
        totals["relations"] += s["relations"]
        totals["blocks"] += s["blocks"]
        totals["blocks_truncated"] += s["blocks_truncated"]
    return {**totals, "by_type": _by_type(), "entities_relinked": len(entity_keys)}


def _by_type() -> dict:
    return {r["rel_type"]: r["n"] for r in
            db.rows("SELECT rel_type, COUNT(*) n FROM relations GROUP BY rel_type")}


def _counts(relations: int, blocks: int, truncated: int) -> dict:
    return {"relations": relations, "by_type": _by_type(),
            "blocks": blocks, "blocks_truncated": truncated}


def rebuild(entity_key: str | None = None) -> dict:
    """Recompute relations. Cheap enough to run after every upload."""
    where = "WHERE verified = 1"
    args: tuple = ()
    if entity_key:
        where += " AND entity_key = ?"
        args = (entity_key,)

    facts = db.rows(f"SELECT * FROM facts {where}", args)
    blocks: dict[tuple[str, str], list[dict]] = {}
    for f in facts:
        blocks.setdefault((f["entity_key"], f["metric_key"]), []).append(f)

    made, truncated = [], 0
    for key, group in blocks.items():
        if len(group) < 2:
            continue
        if len(group) > MAX_BLOCK:
            truncated += 1
            group = sorted(group, key=lambda f: (f["doc_id"], f["page"]))[:MAX_BLOCK]
        for a, b in itertools.combinations(group, 2):
            if a["chunk_id"] == b["chunk_id"]:
                continue  # same page restating itself is not evidence
            res = classify(a, b)
            if not res:
                continue
            made.append((
                str(uuid.uuid4()), a["id"], b["id"], res["rel_type"],
                json.dumps(res.get("axes", [])), res["explanation"],
                json.dumps(res["support"]) if res.get("support") else None,
                1 if a["doc_id"] != b["doc_id"] else 0,
            ))

    # A scoped rebuild has only recomputed one entity's relations, so it must
    # only clear that entity's relations. Clearing the whole table here would
    # delete every other entity's links and put back just this one, which is a
    # silent corpus-wide data loss disguised as an optimisation.
    if entity_key:
        db.write(
            """DELETE FROM relations
                WHERE fact_a IN (SELECT id FROM facts WHERE entity_key = ?)
                   OR fact_b IN (SELECT id FROM facts WHERE entity_key = ?)""",
            (entity_key, entity_key),
        )
    else:
        db.write("DELETE FROM relations")
    db.write_many(
        """INSERT OR IGNORE INTO relations
           (id, fact_a, fact_b, rel_type, axes_differ, explanation, support, cross_doc)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        made,
    )
    counts = {
        r["rel_type"]: r["n"]
        for r in db.rows("SELECT rel_type, COUNT(*) n FROM relations GROUP BY rel_type")
    }
    return {"relations": len(made), "by_type": counts,
            "blocks": len(blocks), "blocks_truncated": truncated}
