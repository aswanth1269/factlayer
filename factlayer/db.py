"""SQLite storage for the fact knowledge layer.

One file, no server. Every fact keeps a hard pointer back to the page and the
verbatim span it came from, so any claim in the UI can be traced to source.
"""

import json
import os
import sqlite3
import threading

DB_PATH = os.environ.get("FACTLAYER_DB") or "factlayer.db"

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    sha256      TEXT NOT NULL UNIQUE,
    pages       INTEGER,
    status      TEXT DEFAULT 'pending',
    detail      TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chunks (
    id          TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL REFERENCES documents(id),
    page        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    text        TEXT NOT NULL,
    findings    TEXT DEFAULT '[]'    -- security findings for this page
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_sha ON chunks(sha256);

-- Cache of raw LLM extraction keyed by chunk content hash.
-- This is what makes re-ingestion and incremental ingestion cheap: the same
-- page never goes to the model twice, even across different uploads.
CREATE TABLE IF NOT EXISTS extraction_cache (
    sha256      TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS facts (
    id              TEXT PRIMARY KEY,
    doc_id          TEXT NOT NULL REFERENCES documents(id),
    chunk_id        TEXT NOT NULL REFERENCES chunks(id),
    page            INTEGER,

    fact_kind       TEXT,            -- measurement | state
    entity          TEXT,
    entity_key      TEXT,
    metric          TEXT,            -- surface form as written in the document
    metric_key      TEXT,            -- canonical key

    value           REAL,
    value_text      TEXT,            -- exactly as printed, e.g. "8,142"
    unit            TEXT,            -- surface form, e.g. "INR crore"
    dimension       TEXT,            -- currency | mass | percent | count | other
    value_base      REAL,            -- value converted into the base unit
    base_unit       TEXT,
    tolerance_base  REAL,            -- rounding tolerance implied by value_text

    state_value     TEXT,            -- for non numeric facts, e.g. "resigned"

    period          TEXT,
    period_start    TEXT,
    period_end      TEXT,
    basis           TEXT,            -- reported | adjusted | pro forma | restated | projected
    scope           TEXT,            -- consolidated | standalone | a segment name
    qualifiers      TEXT DEFAULT '{}',

    evidence_quote  TEXT,
    verified        INTEGER DEFAULT 0,
    reject_reason   TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_facts_block ON facts(entity_key, metric_key);
CREATE INDEX IF NOT EXISTS idx_facts_doc ON facts(doc_id);

CREATE TABLE IF NOT EXISTS relations (
    id           TEXT PRIMARY KEY,
    fact_a       TEXT NOT NULL REFERENCES facts(id),
    fact_b       TEXT NOT NULL REFERENCES facts(id),
    rel_type     TEXT NOT NULL,      -- corroborates | contradicts | reconciled | related
    axes_differ  TEXT DEFAULT '[]',
    explanation  TEXT,
    support      TEXT,               -- json, e.g. an arithmetic reconciliation
    cross_doc    INTEGER DEFAULT 0,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(fact_a, fact_b)
);
CREATE INDEX IF NOT EXISTS idx_rel_type ON relations(rel_type);

-- Full text index over the facts, used for keyword retrieval. SQLite ships
-- FTS5 with BM25 ranking built in, so the layer is searchable without an
-- embedding model or a vector store anywhere in the system.
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    fact_id UNINDEXED,
    entity,
    metric,
    evidence,
    tokenize = 'porter unicode61'
);

-- Surface form -> canonical metric key. Grows as new documents introduce new
-- vocabulary; nothing here is seeded by hand.
CREATE TABLE IF NOT EXISTS metric_aliases (
    surface     TEXT PRIMARY KEY,
    metric_key  TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return _local.conn


# Columns added after the first version shipped. CREATE TABLE IF NOT EXISTS will
# not add them to a database that already exists, so they are applied here.
MIGRATIONS = [
    ("chunks", "findings", "TEXT DEFAULT '[]'"),
]


def init() -> None:
    c = conn()
    c.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    c.commit()


def rows(sql: str, args=()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def write(sql: str, args=()) -> None:
    c = conn()
    c.execute(sql, args)
    c.commit()


def write_many(sql: str, seq) -> None:
    c = conn()
    c.executemany(sql, seq)
    c.commit()


def cache_get(sha: str):
    r = one("SELECT payload FROM extraction_cache WHERE sha256 = ?", (sha,))
    return json.loads(r["payload"]) if r else None


def cache_put(sha: str, payload) -> None:
    write(
        "INSERT OR REPLACE INTO extraction_cache (sha256, payload) VALUES (?, ?)",
        (sha, json.dumps(payload)),
    )


# --------------------------------------------------------------------------
# Keyword retrieval
# --------------------------------------------------------------------------

def index_facts(rows_to_index) -> None:
    """rows_to_index: iterable of (fact_id, entity, metric, evidence)."""
    write_many(
        "INSERT INTO facts_fts (fact_id, entity, metric, evidence) VALUES (?, ?, ?, ?)",
        rows_to_index,
    )


def deindex_document(doc_id: str) -> None:
    ids = [r["id"] for r in rows("SELECT id FROM facts WHERE doc_id = ?", (doc_id,))]
    if ids:
        write_many("DELETE FROM facts_fts WHERE fact_id = ?", [(i,) for i in ids])


def _fts_query(q: str) -> str:
    """Quote each term so user input cannot break FTS5 query syntax."""
    import re as _re

    terms = [t for t in _re.split(r"\W+", q) if t]
    return " OR ".join(f'"{t}"' for t in terms)


def search_facts(q: str, limit: int = 30) -> list[dict]:
    query = _fts_query(q)
    if not query:
        return []
    return rows(
        """SELECT f.*, d.filename, bm25(facts_fts, 0.0, 2.0, 4.0, 1.0) AS rank
           FROM facts_fts
           JOIN facts f ON f.id = facts_fts.fact_id
           JOIN documents d ON d.id = f.doc_id
           WHERE facts_fts MATCH ? AND f.verified = 1
           ORDER BY rank LIMIT ?""",
        (query, limit),
    )
