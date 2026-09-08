"""FastAPI surface. Upload a PDF, then inspect facts, evidence and relations."""

import json
import os
import tempfile

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, guardrails, link, pipeline

app = FastAPI(title="Fact Knowledge Layer")
STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


@app.on_event("startup")
def _startup() -> None:
    db.init()


@app.post("/api/documents")
async def upload(file: UploadFile, max_pages: int | None = None):
    """Ingest one PDF.

    The upload is streamed with a hard size ceiling rather than read whole,
    because an unbounded read here is an unbounded disk write and an unbounded
    model bill for anyone who can reach this route.
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    try:
        size, head = 0, b""
        while chunk := await file.read(1 << 20):
            if not head:
                head = chunk[:8]
            size += len(chunk)
            if size > guardrails.MAX_UPLOAD_BYTES:
                raise guardrails.GuardrailError(
                    f"File exceeds the {guardrails.MAX_UPLOAD_BYTES / 1e6:.0f} MB limit.")
            tmp.write(chunk)
        tmp.close()
        guardrails.check_pdf_bytes(head, size)

        cap = max_pages or guardrails.env_int("FACTLAYER_MAX_PAGES")
        return pipeline.process_pdf(tmp.name, file.filename, max_pages=cap)
    except guardrails.GuardrailError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


@app.get("/api/documents")
def documents():
    return db.rows(
        """SELECT d.*, (SELECT COUNT(*) FROM facts f
                        WHERE f.doc_id = d.id AND f.verified = 1) AS facts
           FROM documents d ORDER BY d.created_at DESC"""
    )


@app.get("/api/facts")
def facts(entity: str | None = None, metric: str | None = None,
          doc_id: str | None = None, verified: int = 1, limit: int = 300):
    sql = "SELECT * FROM facts WHERE verified = ?"
    args: list = [verified]
    if entity:
        sql += " AND entity_key LIKE ?"
        args.append(f"%{entity}%")
    if metric:
        sql += " AND metric_key LIKE ?"
        args.append(f"%{metric}%")
    if doc_id:
        sql += " AND doc_id = ?"
        args.append(doc_id)
    sql += " ORDER BY metric_key, period_start LIMIT ?"
    args.append(limit)
    return db.rows(sql, tuple(args))


@app.get("/api/relations")
def relations(rel_type: str | None = None, cross_doc: int | None = None,
              limit: int = 200):
    sql = """
      SELECT r.id, r.rel_type, r.axes_differ, r.explanation, r.support, r.cross_doc,
             a.id AS a_id, a.metric AS a_metric, a.entity AS a_entity,
             a.value_text AS a_value, a.unit AS a_unit, a.state_value AS a_state,
             a.period AS a_period, a.basis AS a_basis, a.scope AS a_scope,
             a.page AS a_page, a.evidence_quote AS a_quote, da.filename AS a_doc,
             b.id AS b_id, b.metric AS b_metric, b.entity AS b_entity,
             b.value_text AS b_value, b.unit AS b_unit, b.state_value AS b_state,
             b.period AS b_period, b.basis AS b_basis, b.scope AS b_scope,
             b.page AS b_page, b.evidence_quote AS b_quote, db_.filename AS b_doc
      FROM relations r
      JOIN facts a ON a.id = r.fact_a
      JOIN facts b ON b.id = r.fact_b
      JOIN documents da ON da.id = a.doc_id
      JOIN documents db_ ON db_.id = b.doc_id
      WHERE 1=1"""
    args: list = []
    if rel_type:
        sql += " AND r.rel_type = ?"
        args.append(rel_type)
    if cross_doc is not None:
        sql += " AND r.cross_doc = ?"
        args.append(cross_doc)
    sql += " ORDER BY r.cross_doc DESC, r.rel_type LIMIT ?"
    args.append(limit)
    out = db.rows(sql, tuple(args))
    for r in out:
        r["axes_differ"] = json.loads(r["axes_differ"] or "[]")
        r["support"] = json.loads(r["support"]) if r["support"] else None
    return out


@app.get("/api/rejects")
def rejects(limit: int = 100):
    """Facts the verifier threw out. This is the honest failure log."""
    return db.rows(
        """SELECT f.metric, f.value_text, f.unit, f.page, f.evidence_quote,
                  f.reject_reason, d.filename
           FROM facts f JOIN documents d ON d.id = f.doc_id
           WHERE f.verified = 0 ORDER BY f.reject_reason LIMIT ?""",
        (limit,),
    )


@app.get("/api/stats")
def stats():
    total = db.one("SELECT COUNT(*) n FROM facts")["n"]
    ok = db.one("SELECT COUNT(*) n FROM facts WHERE verified = 1")["n"]
    by_type = {r["rel_type"]: r["n"] for r in db.rows(
        "SELECT rel_type, COUNT(*) n FROM relations GROUP BY rel_type")}
    return {
        "documents": db.one("SELECT COUNT(*) n FROM documents")["n"],
        "facts_extracted": total,
        "facts_verified": ok,
        "extraction_precision": round(ok / total, 3) if total else None,
        "relations_by_type": by_type,
        "cross_document_relations": db.one(
            "SELECT COUNT(*) n FROM relations WHERE cross_doc = 1")["n"],
        "metric_vocabulary": db.one("SELECT COUNT(*) n FROM metric_aliases")["n"],
    }


@app.post("/api/relink")
def relink():
    return link.rebuild()


@app.get("/api/lookup")
def lookup(entity: str, metric: str, period: str | None = None):
    """Resolve one cell.

    This is the shape a spreadsheet formula would call. It returns a single
    value, the span it came from, and anything in the corpus that disagrees with
    it, so a cell can carry its own provenance and its own warning instead of
    being a number someone pasted in.
    """
    from . import normalize

    sql = """SELECT f.*, d.filename FROM facts f JOIN documents d ON d.id = f.doc_id
             WHERE f.verified = 1 AND f.entity_key LIKE ? AND f.metric_key LIKE ?"""
    args: list = [f"%{normalize.entity_key(entity)}%", f"%{normalize.metric_slug(metric)}%"]
    if period:
        start, end = normalize.normalize_period(period)
        if start:
            sql += " AND f.period_start = ? AND f.period_end = ?"
            args += [start, end]
    candidates = db.rows(sql + " ORDER BY f.basis = 'reported' DESC LIMIT 20", tuple(args))
    if not candidates:
        return {"found": False, "entity": entity, "metric": metric, "period": period}

    best = candidates[0]
    conflicts = db.rows(
        """SELECT r.rel_type, r.explanation FROM relations r
           WHERE (r.fact_a = ? OR r.fact_b = ?) AND r.rel_type = 'contradicts'""",
        (best["id"], best["id"]),
    )
    return {
        "found": True,
        "value": best["value_text"] or best["state_value"],
        "unit": best["unit"],
        "entity": best["entity"],
        "metric": best["metric"],
        "period": best["period"],
        "basis": best["basis"],
        "scope": best["scope"],
        "source": {"document": best["filename"], "page": best["page"],
                   "evidence": best["evidence_quote"]},
        "contested": bool(conflicts),
        "warnings": [c["explanation"] for c in conflicts],
        "other_readings": len(candidates) - 1,
    }


@app.get("/api/search")
def search(q: str, limit: int = 30):
    """Keyword retrieval over the facts, ranked by BM25.

    There is no embedding model and no vector store anywhere in this system.
    See the README for why that is a decision rather than an omission.
    """
    return db.search_facts(q, limit)


@app.get("/api/security")
def security_findings():
    """Pages flagged during ingestion, and facts quarantined as a result."""
    flagged = []
    for c in db.rows(
        """SELECT c.page, c.findings, d.filename FROM chunks c
           JOIN documents d ON d.id = c.doc_id
           WHERE c.findings != '[]' AND c.findings IS NOT NULL"""
    ):
        for f in json.loads(c["findings"]):
            flagged.append({"document": c["filename"], "page": c["page"], **f})
    quarantined = db.rows(
        """SELECT f.metric, f.value_text, f.page, f.reject_reason, d.filename
           FROM facts f JOIN documents d ON d.id = f.doc_id
           WHERE f.verified = 0 AND f.reject_reason LIKE 'evidence came from hidden%'"""
    )
    # Personal data is reported, not removed. Masking on export is opt-in via
    # FACTLAYER_MASK_PII, because a system that quietly rewrites its own
    # evidence stops being auditable.
    personal = []
    for f in db.rows(
        """SELECT f.metric, f.page, f.evidence_quote, d.filename
           FROM facts f JOIN documents d ON d.id = f.doc_id WHERE f.verified = 1"""
    ):
        kinds = guardrails.find_pii(f["evidence_quote"])
        if kinds:
            personal.append({"document": f["filename"], "page": f["page"],
                             "metric": f["metric"], "kinds": kinds})

    return {"findings": flagged, "quarantined_facts": quarantined,
            "pages_flagged": len({(f["document"], f["page"]) for f in flagged}),
            "personal_data": personal,
            "masking_enabled": guardrails.MASK_PII}


@app.get("/api/export.xlsx")
def export_xlsx():
    from fastapi.responses import Response

    from . import export

    return Response(
        content=export.build(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="fact-knowledge-layer.xlsx"'},
    )


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
