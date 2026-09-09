# Fact knowledge layer

Upload PDFs. The system pulls out facts, keeps each one tied to the exact span
it came from, and then works out whether two facts agree, disagree, or only look
like they disagree.

> TODO before submitting: fill in the demo video link, the real numbers in
> "What it found", and your own honest notes in Limitations.

## Setup and run instructions

Requires Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). If you do
not have uv:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # macOS and Linux
# Windows PowerShell:
# powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then:

```bash
git clone <your repo> factlayer
cd factlayer

uv sync                       # creates .venv and installs everything

cp .env.example .env          # then open .env and add one API key
```

`.env` is read automatically at import, so there is nothing to export by hand.
Set either `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`. If both are present the
OpenAI client is used, and `FACTLAYER_PROVIDER` overrides that.

Check the reasoning engine before spending anything on extraction. This runs
offline with no API key and no PDFs:

```bash
uv run python smoke_test.py
```

It asserts all four of the required cases against a hand-built corpus, checks
the retrieval path, and checks the untrusted-document defences. Exit code 0
means the deterministic core is sound.

To see the injection defences on a real file, build the adversarial sample and
ingest it:

```bash
uv run python make_adversarial_pdf.py     # prints what the scanner catches
uv run python cli.py adversarial-sample.pdf
```

That PDF looks ordinary when opened. Its text layer also carries three
instructions a reader never sees.

Start the server:

```bash
uv run python -m uvicorn factlayer.api:app --reload --port 8000
```

Open http://localhost:8000, choose a PDF, and press "Add to knowledge layer".
A 100 page document takes a couple of minutes on the first pass and is instant
on any later pass, because extraction is cached by page content hash.

To build a corpus from the shell instead, which is faster when preparing a demo:

```bash
uv run python cli.py starter-datasets/delhivery/*.pdf --max-pages 40
uv run python cli.py --relink-only .        # recompute relations, no re-reading
```

### Building the whole layer with no API key

There are two extractors behind one interface, chosen with
`FACTLAYER_EXTRACTOR`. `llm` is the default and reads a page properly.
`rules` reads it with regular expressions over PyMuPDF's text, costs nothing,
and needs no network:

```bash
FACTLAYER_EXTRACTOR=rules uv run python cli.py starter-datasets/delhivery/*.pdf --max-pages 40
```

That builds all three documents in about twelve seconds with zero model calls:
897 verified facts, each one checked against the span it came from, and 36
cross-document reconciliations. `auto` prefers the model and falls back to the
rules only when it returns nothing, which is what a rate-limited provider looks
like from the inside.

The rules extractor is genuinely worse at deciding what a number is *about* —
it takes the nearest label as the metric and has no idea what it means. It is
in the repository because it makes the layer inspectable without an account,
and because it proves the extractor is a swappable part rather than the thing
the system rests on. Both feed the same output contract, the same span
verification, the same coordinate model and the same relation engine.

To start over, delete the database. Nothing else holds state:

```bash
rm -f factlayer.db factlayer.db-wal factlayer.db-shm
```

### Adding a dependency

```bash
uv add <package>              # updates pyproject.toml and uv.lock together
```

Commit `uv.lock`. It is what makes the run reproducible on the reviewer's
machine.

### Without uv

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn factlayer.api:app --port 8000
```

### Routes

| Route | What it does |
|---|---|
| `POST /api/documents` | upload a PDF, returns extraction and linking stats |
| `GET /api/facts` | facts with their coordinates and evidence |
| `GET /api/relations?rel_type=` | corroborates, contradicts, reconciled, related |
| `GET /api/rejects` | facts the verifier threw out, with the reason |
| `GET /api/stats` | corpus level counts and extraction precision |
| `GET /api/lookup?entity=&metric=&period=` | resolve a single cell, with its evidence and any warnings |
| `GET /api/search?q=` | keyword retrieval over facts and evidence, ranked by BM25 |
| `GET /api/security` | pages flagged at ingestion, and facts quarantined as a result |
| `GET /api/export.xlsx` | the whole layer as a workbook, evidence attached to each value cell |

Nothing in the code is specific to the starter documents. There are no
hard-coded metric names, filenames, entities or schemas.

## Video demo

TODO: link, 3 minutes or less.

## Approach

### A fact is a measurement plus the coordinates it was measured in

The obvious design is to extract facts, embed them, and call two similar facts
with different numbers a contradiction. That fails badly on real filings,
because almost every apparent conflict in a financial or institutional document
is a period, basis, or scope difference rather than a disagreement. It produces
a wall of false positives and no explanation.

So a fact here is not a subject-predicate-value triple. It is a value plus the
axes that make it comparable:

```
entity     Delhivery Limited, India
metric     revenue from services, EBITDA, freight tonnage, CPI inflation
period     FY24, Q4 FY24, as of 31 March 2024
unit       INR crore, million tons, percent
basis      reported, adjusted, pro forma, restated, projected
scope      consolidated, standalone, Express Parcel
qualifiers open bag for anything else the page attaches
```

With coordinates attached, the relation logic becomes a comparison rather than a
similarity score:

- same coordinates and the values agree, within the rounding each figure's own
  precision implies, is **corroboration**
- same coordinates and the values disagree is a **contradiction**
- different coordinates is **reconciled**, and the system names the axis that
  differs

That last branch is the interesting one and it is the reason the output is
readable. "These differ because the basis axis is reported against adjusted,
everything else matches" is reasoning. A cosine number is not.

### Where the model is used and where it is not

The model does two jobs only: reading a page into the schema above, and
clustering metric surface forms into canonical keys. Every comparison,
conversion, tolerance and contradiction decision is plain Python in
`normalize.py` and `link.py`.

That split is deliberate. It means every claim the UI makes about two facts can
be traced to code you can read, and the failure modes are debuggable rather than
prompt-shaped.

### Grounding is verified, not assumed

The extractor is asked to copy the number and a short evidence span verbatim.
Every fact is then checked against the page it claims to come from: the quote
must appear on the page, and the printed digits must appear inside the quote.
Anything that fails is stored with a rejection reason and never enters the
knowledge layer.

This turns "extraction is imperfect" from an apology into a measured number.
The corpus level extraction precision is on the stats bar and in `/api/stats`,
and every rejected fact is browsable under "Rejected extractions".

### Rounding-aware comparison

Two figures are treated as the same quantity when their gap fits inside the
rounding each of them implies. `1.4` was printed to one decimal, so anything
within `0.05` million tons is the same number. `1,429` was printed exactly, so
its tolerance is half a unit.

This is what lets "1.4 Mn tons" corroborate "1,429 '000 tons" across two pages
without a fuzzy similarity score anywhere in the pipeline.

### Trying to explain a contradiction before reporting one

When two figures for the same metric disagree with no axis to explain it, the
system searches the store for a third fact whose value equals the gap. When it
finds one, the unexplained difference becomes an arithmetic identity backed by
three separate pieces of evidence, and the relation is downgraded from
contradiction to reconciled with the bridging fact shown in the UI.

### Other engineering decisions

**Page-level chunks.** Most facts in these documents live in tables, and a
fixed-size character window cuts tables away from their header rows. A page is
the smallest unit that reliably keeps a table intact and it gives every fact a
page number a human can check.

**Blocking instead of a vector index.** Facts are only compared inside a block
keyed on entity and canonical metric. That keeps the pairwise cost small and
removes the need for a vector database entirely.

**Extraction cached by page content hash.** Re-uploading a document, or adding a
fourth one, never re-reads a page that has already been read. This is what makes
incremental ingestion cheap.

**Metric vocabulary grows from the documents.** New surface forms are sent to
the model once, mapped onto existing canonical keys or given new ones, and
cached in SQLite. Nothing is seeded by hand, so a document from an unrelated
domain brings its own vocabulary with it.

**SQLite.** One file, no service to run, and the whole knowledge layer is
inspectable with a shell. For a corpus of this size a heavier store would buy
nothing.

**Generic page ranking.** When a page cap is set, pages are ranked by digit
density, currency and percent markers and period-like tokens. All generic
signals, no document-specific keywords, so the ranking survives an unfamiliar
PDF.

### Retrieval without vectors

There is no embedding model, no vector store, and no cosine similarity anywhere
in this system. That is a decision, and the reasoning is worth stating because
reaching for a vector database is the reflex here.

The matching problem in this project is not semantic recall over a large corpus.
It is deciding whether two specific figures are talking about the same thing.
Similarity is exactly the wrong tool for that: "revenue from services" and
"total revenue from operations" are highly similar and mean different things,
while "1.4 Mn Tons" and "1,429" are textually unrelated and mean the same thing.
An embedding gets both of those backwards.

What the system uses instead:

- **Deterministic blocking.** Facts are only compared inside a block keyed on
  entity and canonical metric. Candidate generation is an index lookup, not a
  nearest-neighbour search, and it is exact.
- **Lexical pre-grouping.** Surface forms that reduce to the same token set are
  merged in Python before any model is involved, so identical vocabulary never
  costs a call.
- **Long context in place of embedding.** The thing you would normally embed
  here is the metric vocabulary, and it runs to a few hundred short strings.
  That fits in one prompt, so the model clusters the vocabulary directly and the
  result is cached in SQLite. Embeddings solve a scale problem this corpus does
  not have.
- **BM25 for keyword retrieval.** SQLite ships FTS5 with BM25 ranking, so
  `/api/search` is full text search over metric names, entities and evidence
  spans with no extra service and no extra dependency.

The honest trade-off: the layer cannot answer a vague semantic query like "what
do we know about profitability" and pull back loosely related facts. It answers
"what does this corpus say about this metric, and does it agree with itself".
That is the job the assignment describes, and a system that cannot be precise
about metric identity cannot do it at all.

### Guardrails

Guardrails here mean constraining the system's own behaviour: what the model is
allowed to return, what the pipeline is allowed to ingest, and what it is
allowed to spend. Two of these closed real holes.

**A typed contract on model output.** The extractor previously trusted whatever
JSON came back and wrote it to the database. Nothing now reaches storage until
it validates against a Pydantic model, so a missing field, a wrong type, or a
hundred hallucinated facts on one page become rejected facts with a reason
rather than corrupt rows. The validator also strips footnote markers glued to
values, which is a failure mode visible in the starter documents.

**A bounded ingest boundary.** The upload route previously read a file of any
size and opened every page of it. That is an unbounded disk write and an
unbounded model bill for anyone who can reach the endpoint. Uploads are now
streamed against a size ceiling, checked for a real PDF header, and refused if
encrypted or beyond a hard page limit.

**A spend ceiling per document.** One upload cannot trigger an unlimited number
of model calls. The budget is enforced before each call and reported in the
upload response, and cache hits do not count against it.

**Retries that know the difference.** Rate limits and timeouts back off and
retry. A malformed request does not, because retrying it just spends money
producing the same error.

**Personal data is reported, not removed.** Filings name people, and evidence
spans about directors carry contact details and identifiers that end up in an
exported workbook. Those spans are detected and listed. Masking is opt-in via
`FACTLAYER_MASK_PII`, because a system that quietly rewrites its own evidence
stops being auditable, and auditability is the entire point of this project.

Every guardrail fails loudly and locally. One that silently drops work is worse
than none, because you stop being able to tell a quiet system from a broken one.
`smoke_test.py` covers all of them.

### Uploaded PDFs are untrusted input

The brief says the system will be tested with documents it has never seen. Every
uploaded PDF therefore flows straight from an unknown source into a model
prompt, which is the standard setup for indirect prompt injection. A PDF can
carry text a human reader never sees: drawn in invisible render mode, at zero
alpha, white on a white page, or at two points. The extracted text layer
contains all of it.

Three defences, in order of how much they actually buy.

**The architecture does most of the work.** Every fact is verified against the
span it claims to come from before it is stored. An injected instruction that
persuades the model to emit a fabricated figure produces a fact whose value does
not appear in its own evidence, and it is dropped. This check was already there
for accuracy, and it turns out to be the strongest thing standing between a
malicious document and the knowledge layer.

**Detection at ingestion.** Every page is scanned for text a reader cannot see
and for language addressed to a model rather than a reader. Findings are stored
against the page, any fact whose evidence overlaps hidden text is quarantined,
and everything flagged is browsable under "Flagged content". Nothing is blocked.
A document that trips these checks is still read, because silently refusing a
legitimate filing is a worse failure than flagging one.

**Separation and containment.** Page content reaches the model inside explicit
delimiters, with an instruction that nothing within them is an instruction. The
extractor can only emit facts in a fixed schema, and every one is verified, so
even a successful injection has a narrow blast radius.

`make_adversarial_pdf.py` builds a document carrying all three hidden channels
and the scanner catches each of them.

**One more, specific to the spreadsheet export.** Document text ends up in cells,
and a spreadsheet treats a string beginning with `=`, `+`, `-` or `@` as a
formula. Untrusted text from an uploaded PDF could therefore become executable
content in a workbook someone else opens. Every string written to a cell is
prefixed so it stays text.

### Getting the facts where the work actually happens

Extracted facts are worth little while they sit in a web page. The analyst
reading a prospectus is building a model, and the model is a spreadsheet.

So the layer exports to a workbook where every value cell carries a comment
holding the verbatim span, the document and the page it came from. A number in
that sheet is never a number someone pasted in; hovering it shows the sentence
in the filing that justifies it. The reconciliation tab puts both figures, both
contexts, the axis that differs and the explanation on a single row, colour
coded by relationship.

There is also a cell-shaped read path:

```
GET /api/lookup?entity=Delhivery&metric=revenue+from+services&period=FY24

{
  "value": "8,142", "unit": "INR crore", "basis": "reported",
  "source": { "document": "...deck.pdf", "page": 5, "evidence": "..." },
  "contested": false, "other_readings": 1
}
```

That is the shape a spreadsheet custom function would call. A cell can then
carry its own provenance and its own warning when the corpus disagrees with
itself, which is the part a plain extraction tool cannot do.

### AI tools used

TODO: list what you actually used and for what.

## The four cases

TODO: fill these in from your own run and screenshot each one.

**1. Corroborated across documents.**
TODO. Good candidate: PTL freight tonnage FY24, stated as `1.4 Mn Tons` on page
5 of the earnings deck and as `1,429` thousand tons on page 8. Different unit,
different page, same quantity.

**2. Genuine or likely contradiction.**
TODO. Look at prior-year figures restated in the FY24 annual report against the
same figures as first reported, or run the macro dataset where the IMF and RBI
publish different projections for the same year.

**3. Apparent contradiction explained by context.**
TODO. Good candidates: EBITDA `₹127 Cr` against Adjusted EBITDA `₹76 Cr` for
FY24 (basis axis), revenue from services against total revenue from operations
(the arithmetic bridge), or FY22 tonnage on a pro forma basis including a full
year of Spoton against the prospectus figure for the same year.

## Limitations and next steps

TODO: be specific and honest. Some real ones already visible:

- PyMuPDF's reading order interleaves chart labels and values on the FY24
  performance page of the earnings deck, so two adjacent series can be attached
  to the wrong captions. Span verification catches a fabricated number but it
  cannot catch a correctly copied number with the wrong label. A layout-aware
  reader, or a bbox-proximity check between a value and its caption, would fix
  this.
- Footnote markers printed next to figures sometimes get glued onto values.
- Facts are compared only within an entity and canonical metric block, so a fact
  stated about a subsidiary and a fact stated about the group never meet.
- Contradiction detection has no notion of source authority. A regulator and a
  company press release are weighted the same.
- Multi-page tables are split at the page boundary and the continuation loses
  its header row.
- No incremental relinking. Relations are recomputed for the whole store after
  each upload, which is fine at this size but would not scale.
- Injection screening is pattern based, so it catches phrasing it has seen and
  misses paraphrase. A document that legitimately discusses prompt injection
  would also trip it. Detection is deliberately advisory rather than blocking
  for that reason.
- Hidden text detection assumes a white page. Text matching a coloured
  background would not be caught.
- BM25 does not help when two documents describe the same metric with no shared
  vocabulary at all. The metric canonicalization pass is the only thing bridging
  that, and it sees surface strings without their surrounding context.
- Guardrail limits are fixed numbers from environment variables rather than
  anything adaptive. A budget in model calls is a poor proxy for a budget in
  money, since call cost varies with page length.
- PII detection is regex based and tuned for Indian filings. It will miss names
  and postal addresses entirely, which are the most common personal data in
  these documents and the hardest to detect without false positives.
- The spreadsheet export is one way. Correcting a fact in the sheet does not
  write back into the layer, and a two way sync is the obvious next step.

## Additional notes

TODO.
