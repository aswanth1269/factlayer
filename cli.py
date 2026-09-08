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
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="cap on pages read per PDF, highest fact-density first")
    ap.add_argument("--relink-only", action="store_true",
                    help="recompute relations without re-reading any PDF")
    args = ap.parse_args()

    db.init()
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
