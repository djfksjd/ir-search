#!/usr/bin/env python3
"""Compare two survey runs and report what changed.

Reads every raw-crawl *.jsonl in a previous and a current survey directory
(diff artifacts like new_items.jsonl / screening* / report* are skipped) and
classifies:

  new        — announcements that appeared since the previous run
  closed     — announcements that disappeared (deadline passed / pulled)
  changed    — same announcement, but title / apply_start / apply_end /
               status differ (the changed fields are listed per item)
  unchanged  — still open, same fields (carry over previous verdicts)

Records are keyed by (source, id) so K-Startup and the other sources never
collide. Sources crawled only in one of the two runs are excluded from the
closed/new comparison (a source you didn't re-crawl isn't "all closed") and
reported separately, so coverage mismatches can't masquerade as changes.

Profile fingerprint (optional, recommended):
  --old-profile / --new-profile point at the ir-search-profile.md used for
  each run (markdown "- 키: 값" bullets). If the judgment axes (창업 단계,
  지역 연고, 대표자, 필요한 것) differ — or only one profile is given, or a
  profile can't be parsed — UNCHANGED carry-over is INVALIDATED (fail-closed)
  and every current record is written to --out for full re-review.

Usage:
  python3 diff_surveys.py <prev_dir> <curr_dir> [--out new_items.jsonl] \
      [--old-profile prev/ir-profile-snapshot.md --new-profile ir-search-profile.md]

Output: human-readable summary on stdout; with --out, the records needing
review (new + changed + new-source items; ALL records when carry-over is
invalidated) are written as jsonl for the detail crawlers.
Exit code: 0 on success (even if nothing changed), 1 on bad input
(0 current records, broken JSON line, duplicate key, missing dir).
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

COMPARE_FIELDS = ["title", "apply_start", "apply_end", "status"]

# ir-search-profile.md bullets that define the judgment axes. If any of these
# change, previous A/B/C verdicts can no longer be carried over.
PROFILE_AXES = ["창업 단계", "지역 연고", "대표자", "필요한 것"]

# diff/screening artifacts that live in survey folders but are NOT raw crawls
SKIP_FILES = {"new_items.jsonl"}
SKIP_PREFIXES = ("new_items", "screening", "report")


def load_dir(d: Path):
    """Load every raw-crawl *.jsonl in *d* into {(source, id): record}.

    Fail-closed: broken JSON lines and duplicate keys abort with exit 1.
    """
    records = {}
    files = [
        f for f in sorted(d.glob("*.jsonl"))
        if f.name not in SKIP_FILES and not f.name.startswith(SKIP_PREFIXES)
    ]
    if not files:
        sys.exit(f"ERROR: no raw-crawl .jsonl files in {d}")
    for f in files:
        for ln, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                sys.exit(f"ERROR: broken JSON at {f}:{ln} — {e}")
            if "kind" in rec and "record" in rec:
                continue  # stray diff artifact record — not a raw crawl row
            # kstartup_crawl.py records have pbancSn/start/deadline and no
            # source field; sources_crawl.py records have source/id/apply_*.
            if "pbancSn" in rec:
                key = ("kstartup", str(rec["pbancSn"]))
                rec.setdefault("source", "kstartup")
                rec.setdefault("apply_start", rec.get("start", ""))
                rec.setdefault("apply_end", rec.get("deadline", ""))
            elif "source" in rec and "id" in rec:
                key = (rec["source"], str(rec["id"]))
            else:
                continue  # unrecognized record shape
            if key in records:
                sys.exit(
                    f"ERROR: duplicate key {key} at {f}:{ln} — the same "
                    "announcement was loaded twice (overlapping jsonl files?)"
                )
            records[key] = rec
    return records


def parse_profile_bullets(path):
    """Parse '- 키: 값' bullet lines from an ir-search-profile.md."""
    fields = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return fields
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("-"):
            continue
        s = s.lstrip("-").strip()
        if ":" in s:
            k, v = s.split(":", 1)
            fields[k.strip()] = v.strip()
    return fields


def profile_fingerprint(fields):
    payload = json.dumps({k: fields.get(k) for k in PROFILE_AXES},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def changed_fields(old, new):
    return [f for f in COMPARE_FIELDS if (old.get(f) or None) != (new.get(f) or None)]


def fmt(rec):
    end = rec.get("apply_end") or "?"
    return (f"[{rec.get('source')}] {rec.get('title', '(no title)')} — 마감 {end}"
            f"\n    {rec.get('url', '')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("prev_dir", type=Path, help="previous survey directory")
    ap.add_argument("curr_dir", type=Path, help="current survey directory")
    ap.add_argument("--out", type=Path, help="write items needing review as jsonl")
    ap.add_argument("--old-profile", help="profile snapshot used for prev_dir")
    ap.add_argument("--new-profile", help="profile used for curr_dir")
    args = ap.parse_args()

    for d in (args.prev_dir, args.curr_dir):
        if not d.is_dir():
            sys.exit(f"ERROR: not a directory: {d}")

    prev = load_dir(args.prev_dir)
    curr = load_dir(args.curr_dir)
    if not curr:
        sys.exit("ERROR: current run has 0 records — refusing to diff "
                 "(a failed crawl would look like everything closed)")

    # ---- profile fingerprint validation (fail-closed) --------------------
    invalidate = False
    if bool(args.old_profile) != bool(args.new_profile):
        invalidate = True
        print("WARNING: 프로필 인자가 한쪽만 지정됐다 — 승계 무효(fail-closed), "
              "전체 재검토", file=sys.stderr)
    elif args.old_profile and args.new_profile:
        old_fields = parse_profile_bullets(args.old_profile)
        new_fields = parse_profile_bullets(args.new_profile)
        # 판정 축(PROFILE_AXES)이 하나도 없는 프로필은 파싱 실패와 같다 — 무관한
        # 불릿만 있는 파일 두 개가 "동일 fingerprint"로 승계를 통과하면 안 된다.
        old_axes = any(old_fields.get(k) for k in PROFILE_AXES)
        new_axes = any(new_fields.get(k) for k in PROFILE_AXES)
        if not old_fields or not new_fields or not old_axes or not new_axes:
            invalidate = True
            print("WARNING: 프로필 파일을 읽지 못했거나 판정 축(창업 단계·지역 등)이 "
                  "비어 있다 — 승계 무효(fail-closed), 전체 재검토", file=sys.stderr)
        elif profile_fingerprint(old_fields) != profile_fingerprint(new_fields):
            invalidate = True
            print("WARNING: profile changed — 전체 재판정 필요 "
                  "(판정 축이 달라져 UNCHANGED 승계를 무효화한다)", file=sys.stderr)
    else:
        print("NOTE: 프로필 미지정 — 판정 승계 유효성(fingerprint)이 검증되지 "
              "않았다. --old-profile/--new-profile 지정 권장", file=sys.stderr)

    prev_sources = {k[0] for k in prev}
    curr_sources = {k[0] for k in curr}
    common = prev_sources & curr_sources

    new = [curr[k] for k in curr if k not in prev and k[0] in common]
    closed = [prev[k] for k in prev if k not in curr and k[0] in common]
    changed = [
        (prev[k], curr[k], changed_fields(prev[k], curr[k]))
        for k in curr
        if k in prev and changed_fields(prev[k], curr[k])
    ]
    unchanged = sum(
        1 for k in curr if k in prev and not changed_fields(prev[k], curr[k])
    )
    added_sources = sorted(curr_sources - prev_sources)
    dropped_sources = sorted(prev_sources - curr_sources)
    first_time = [curr[k] for k in curr if k[0] in added_sources]

    print(f"# Survey diff: {args.prev_dir.name} → {args.curr_dir.name}")
    print(f"prev {len(prev)} items / curr {len(curr)} items "
          f"(sources compared: {', '.join(sorted(common)) or 'none'})\n")

    if invalidate:
        print("## CARRY-OVER INVALIDATED — 프로필(판정 축)이 바뀌었거나 검증 불가.")
        print("   UNCHANGED 승계 금지: 아래 분류와 무관하게 전건을 재검토하라.\n")

    print(f"## NEW ({len(new)}) — need full review + detail verification")
    for r in sorted(new, key=lambda r: r.get("apply_end") or "~"):
        print(f"  + {fmt(r)}")

    print(f"\n## CHANGED ({len(changed)}) — same announcement, fields differ")
    for old, cur, flds in changed:
        was = ", ".join(f"{f}: {old.get(f) or '?'} → {cur.get(f) or '?'}" for f in flds)
        print(f"  ~ {fmt(cur)}\n    changed_fields: {flds} ({was})")

    print(f"\n## CLOSED ({len(closed)}) — gone since previous run")
    for r in closed:
        print(f"  - [{r.get('source')}] {r.get('title', '(no title)')}")

    if invalidate:
        print(f"\n## UNCHANGED: {unchanged} items — 승계 불가(프로필 변경), 전건 재검토")
    else:
        print(f"\n## UNCHANGED: {unchanged} items (carry over previous verdicts)")

    if added_sources:
        print(f"\n## NEW SOURCES this run ({', '.join(added_sources)}): "
              f"{len(first_time)} items — no baseline, review all of them")
    if dropped_sources:
        print(f"\n## WARNING — sources in previous run but not re-crawled: "
              f"{', '.join(dropped_sources)} (their items were NOT diffed)")

    if args.out:
        if invalidate:
            out_items = list(curr.values())  # full re-review
        else:
            out_items = new + [cur for _, cur, _ in changed] + first_time
        with open(args.out, "w", encoding="utf-8") as f:
            for r in out_items:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\nWrote {len(out_items)} items to review → {args.out}"
              + (" (ALL current records — carry-over invalidated)" if invalidate else ""))


if __name__ == "__main__":
    main()
