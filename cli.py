"""Batch ingest from the command line.

    python cli.py starter-datasets/delhivery/*.pdf --max-pages 40

Useful for building the corpus before recording a demo, since the API route
does the same work but one file at a time.
"""

import argparse
import json
import os
import sys

from factlayer import db, link, pipeline


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", default=["."])
    ap.add_argument("--max-pages", type=int, default=None,
                    help="cap on pages read per PDF, highest fact-density first")
    ap.add_argument("--relink-only", action="store_true",
                    help="recompute relations without re-reading any PDF")
    ap.add_argument("--reset", action="store_true",
                    help="clear facts and relations but keep the extraction "
                         "cache, so re-running costs no model calls")
    args = ap.parse_args()

    db.init()
    if args.reset:
        kept = db.one("SELECT COUNT(*) n FROM extraction_cache")["n"]
        db.write("DELETE FROM relations")
        db.write("DELETE FROM facts_fts")
        db.write("DELETE FROM facts")
        db.write("DELETE FROM chunks")
        db.write("DELETE FROM documents")
        db.write("DELETE FROM metric_aliases")
        print(f"Cleared facts and relations. {kept} cached page(s) kept, so "
              "re-ingesting the same PDFs costs nothing.")
        if not args.paths or args.paths == ["."]:
            return 0

    if args.relink_only:
        print(json.dumps(link.rebuild(), indent=2))
        return 0

    for p in args.paths:
        if not os.path.exists(p):
            print(f"missing: {p}", file=sys.stderr)
            continue
        result = pipeline.process_pdf(p, os.path.basename(p), max_pages=args.max_pages)
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())