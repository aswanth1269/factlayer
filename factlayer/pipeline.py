"""Ingest -> extract -> verify -> normalize -> link, for one PDF."""

import json
import os
import uuid

from . import db, extract, guardrails, ingest, link, normalize, security


def process_pdf(path: str, filename: str, max_pages: int | None = None) -> dict:
    db.init()
    file_hash = ingest.file_sha256(path)

    existing = db.one("SELECT * FROM documents WHERE sha256 = ?", (file_hash,))
    if existing and existing["status"] == "ready":
        return {"doc_id": existing["id"], "skipped": "already ingested",
                "filename": existing["filename"]}

    doc_id = existing["id"] if existing else str(uuid.uuid4())
    if not existing:
        db.write(
            "INSERT INTO documents (id, filename, sha256, status) VALUES (?, ?, ?, 'processing')",
            (doc_id, filename, file_hash),
        )
    else:
        db.write("UPDATE documents SET status='processing' WHERE id=?", (doc_id,))

    total_pages, chunks = ingest.read_pdf(path, max_pages=max_pages)
    db.deindex_document(doc_id)
    db.write("DELETE FROM facts WHERE doc_id = ?", (doc_id,))
    db.write("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    db.write("UPDATE documents SET pages = ? WHERE id = ?", (total_pages, doc_id))

    chunk_rows = []
    for c in chunks:
        c["id"] = str(uuid.uuid4())
        c["doc_id"] = doc_id
        chunk_rows.append((c["id"], doc_id, c["page"], c["sha256"], c["text"],
                           json.dumps(c.get("findings") or [])))
    db.write_many(
        "INSERT INTO chunks (id, doc_id, page, sha256, text, findings) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        chunk_rows,
    )
    flagged = sum(1 for c in chunks if c.get("findings"))
    if flagged:
        print(f"[{filename}] {flagged} chunk(s) carry security findings")

    print(f"[{filename}] {total_pages} pages, {len(chunks)} chunks queued")
    budget = guardrails.Budget()
    workers = guardrails.env_int("FACTLAYER_WORKERS", 8)
    pairs = extract.extract_all(chunks, workers=workers, budget=budget)

    raw, kept, rejected = [], [], []
    for chunk, facts in pairs:
        for f in facts:
            raw.append((chunk, f))
            ok, reason = extract.verify(f, chunk["text"])
            if ok:
                hidden = security.evidence_is_hidden(
                    f.get("evidence_quote", ""), chunk.get("findings") or [])
                if hidden:
                    ok, reason = False, hidden
            (kept if ok else rejected).append((chunk, f, reason))

    # One canonicalization pass over the metric vocabulary introduced here.
    surfaces = [f.get("metric") or "" for _, f, _ in kept]
    alias = extract.canonicalize_metrics(surfaces)

    to_insert = []
    for chunk, f, _ in kept:
        to_insert.append(_row(doc_id, chunk, f, alias, verified=1, reason=None))
    for chunk, f, reason in rejected:
        to_insert.append(_row(doc_id, chunk, f, alias, verified=0, reason=reason))

    db.write_many(
        """INSERT INTO facts (
            id, doc_id, chunk_id, page, fact_kind, entity, entity_key, metric,
            metric_key, value, value_text, unit, dimension, value_base, base_unit,
            tolerance_base, state_value, period, period_start, period_end, basis,
            scope, qualifiers, evidence_quote, verified, reject_reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        to_insert,
    )

    db.index_facts([
        (r[0], r[5], r[7], r[23]) for r in to_insert if r[24] == 1
    ])

    # Adding a document can only change relations for the entities that document
    # actually mentions. Everything else in the corpus is already settled, so it
    # is re-linked only on an explicit full rebuild. This is what keeps the cost
    # of the eleventh upload the same as the cost of the second, instead of
    # growing with the square of the corpus.
    touched = sorted({r[6] for r in to_insert if r[24] == 1})
    stats = link.rebuild_entities(touched)
    precision = round(len(kept) / len(raw), 3) if raw else None
    detail = (f"{len(raw)} extracted, {len(kept)} verified, {len(rejected)} rejected")
    db.write("UPDATE documents SET status='ready', detail=? WHERE id=?", (detail, doc_id))

    return {
        "doc_id": doc_id,
        "filename": filename,
        "total_pages": total_pages,
        "pages_read": len({c["page"] for c in chunks}),
        "facts_extracted": len(raw),
        "facts_verified": len(kept),
        "facts_rejected": len(rejected),
        "extraction_precision": precision,
        "rejection_reasons": _tally(rejected),
        "guardrails": budget.report(),
        **stats,
    }


def _tally(rejected) -> dict:
    out: dict[str, int] = {}
    for _, _, reason in rejected:
        out[reason or "unknown"] = out.get(reason or "unknown", 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _row(doc_id, chunk, f, alias, verified, reason):
    metric = (f.get("metric") or "").strip()
    entity = (f.get("entity") or "").strip()
    unit = (f.get("unit") or "").strip()
    value_text = (f.get("value_text") or "").strip()

    dimension, base_unit, mult = normalize.normalize_unit(unit)
    value = normalize.parse_number(value_text)
    value_base = value * mult if value is not None else None
    tol_base = normalize.rounding_tolerance(value_text) * mult
    p_start, p_end = normalize.normalize_period(f.get("period"))

    return (
        str(uuid.uuid4()), doc_id, chunk["id"], chunk["page"],
        f.get("fact_kind") or "measurement",
        entity, normalize.entity_key(entity),
        metric, alias.get(metric.lower()) or normalize.metric_slug(metric),
        value, value_text, unit, dimension, value_base, base_unit, tol_base,
        (f.get("state_value") or "").strip() or None,
        f.get("period"), p_start, p_end,
        normalize.normalize_basis(f.get("basis")),
        normalize.normalize_scope(f.get("scope")),
        json.dumps(f.get("qualifiers") or {}),
        (f.get("evidence_quote") or "").strip(),
        verified, reason,
    )
