#!/usr/bin/env python3
"""Compare two survey runs and report what changed.

Reads every *.jsonl in a previous and a current survey directory (the raw
crawl output that the skill saves alongside each report) and classifies:

  new              — announcements that appeared since the previous run
  closed           — announcements that disappeared (deadline passed / pulled)
  deadline_changed — same announcement, different deadline (extension etc.)
  unchanged        — still open, same deadline (carry over previous verdicts)

Records are keyed by (source, id) so K-Startup and the other sources never
collide. Sources crawled only in one of the two runs are excluded from the
closed/new comparison (a source you didn't re-crawl isn't "all closed") and
reported separately, so coverage mismatches can't masquerade as changes.

Usage:
  python3 diff_surveys.py <prev_dir> <curr_dir> [--out new_items.jsonl]

Output: human-readable summary on stdout; with --out, the new items are
written as jsonl so they can be fed straight to the detail crawlers.
Exit code: 0 on success (even if nothing changed), 1 on bad input.
"""

import argparse
import json
import sys
from pathlib import Path


def load_dir(d: Path):
    """Load every *.jsonl in *d* into {(source, id): record}."""
    records = {}
    files = sorted(d.glob("*.jsonl"))
    if not files:
        sys.exit(f"ERROR: no .jsonl files in {d}")
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # kstartup_crawl.py records have pbancSn/deadline and no source
            # field; sources_crawl.py records have source/id/apply_end.
            if "pbancSn" in rec:
                key = ("kstartup", str(rec["pbancSn"]))
                rec.setdefault("source", "kstartup")
                rec.setdefault("apply_end", rec.get("deadline", ""))
            elif "source" in rec and "id" in rec:
                key = (rec["source"], str(rec["id"]))
            else:
                continue  # unrecognized record shape
            records[key] = rec
    return records


def fmt(rec):
    end = rec.get("apply_end") or "?"
    return f"[{rec.get('source')}] {rec.get('title', '(no title)')} — 마감 {end}\n    {rec.get('url', '')}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("prev_dir", type=Path, help="previous survey directory")
    ap.add_argument("curr_dir", type=Path, help="current survey directory")
    ap.add_argument("--out", type=Path, help="write new items as jsonl")
    args = ap.parse_args()

    for d in (args.prev_dir, args.curr_dir):
        if not d.is_dir():
            sys.exit(f"ERROR: not a directory: {d}")

    prev = load_dir(args.prev_dir)
    curr = load_dir(args.curr_dir)

    prev_sources = {k[0] for k in prev}
    curr_sources = {k[0] for k in curr}
    common = prev_sources & curr_sources

    new = [curr[k] for k in curr if k not in prev and k[0] in common]
    closed = [prev[k] for k in prev if k not in curr and k[0] in common]
    deadline_changed = [
        (prev[k], curr[k])
        for k in curr
        if k in prev and (curr[k].get("apply_end") or "") != (prev[k].get("apply_end") or "")
    ]
    unchanged = sum(
        1 for k in curr
        if k in prev and (curr[k].get("apply_end") or "") == (prev[k].get("apply_end") or "")
    )
    added_sources = sorted(curr_sources - prev_sources)
    dropped_sources = sorted(prev_sources - curr_sources)
    first_time = [curr[k] for k in curr if k[0] in added_sources]

    print(f"# Survey diff: {args.prev_dir.name} → {args.curr_dir.name}")
    print(f"prev {len(prev)} items / curr {len(curr)} items "
          f"(sources compared: {', '.join(sorted(common)) or 'none'})\n")

    print(f"## NEW ({len(new)}) — need full review + detail verification")
    for r in sorted(new, key=lambda r: r.get("apply_end") or "~"):
        print(f"  + {fmt(r)}")

    print(f"\n## DEADLINE CHANGED ({len(deadline_changed)})")
    for old, cur in deadline_changed:
        print(f"  ~ {fmt(cur)}\n    (was: {old.get('apply_end') or '?'})")

    print(f"\n## CLOSED ({len(closed)}) — gone since previous run")
    for r in closed:
        print(f"  - [{r.get('source')}] {r.get('title', '(no title)')}")

    print(f"\n## UNCHANGED: {unchanged} items (carry over previous verdicts)")

    if added_sources:
        print(f"\n## NEW SOURCES this run ({', '.join(added_sources)}): "
              f"{len(first_time)} items — no baseline, review all of them")
    if dropped_sources:
        print(f"\n## WARNING — sources in previous run but not re-crawled: "
              f"{', '.join(dropped_sources)} (their items were NOT diffed)")

    if args.out:
        out_items = new + first_time
        with open(args.out, "w", encoding="utf-8") as f:
            for r in out_items:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\nWrote {len(out_items)} items to review → {args.out}")


if __name__ == "__main__":
    main()
