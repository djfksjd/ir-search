#!/usr/bin/env python3
"""K-Startup announcement crawler — bundled with the ir-search skill.

Accesses only public announcement pages (currently-recruiting list).
No login, no private areas. A polite delay is applied between requests.

Usage:
  # Collect ALL currently-recruiting announcements (JSONL)
  python3 kstartup_crawl.py list -o kstartup_all.jsonl

  # Save detail-page text (for eligibility verification)
  python3 kstartup_crawl.py detail 178481 178215 -o details/

Dependency: curl_cffi>=0.15 recommended (passes TLS-fingerprint checks).
Falls back to the standard urllib; if blocked, an install hint is printed.

Exit codes (fail-closed contract):
  0  full success — collection complete
  2  partial / suspicious — network error mid-crawl (partial jsonl saved),
     page 1 parsed 0 items (site structure change), total below the minimum
     expectation (~250 open announcements is normal, <50 is suspicious),
     page cap reached while new items were still appearing, or (detail mode)
     one or more detail fetches failed.
Callers MUST treat exit 2 as incomplete coverage, never as a clean success.
"""
import argparse
import html as htmllib
import json
import os
import re
import sys
import time

BASE = "https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do"
DETAIL_URL = BASE + "?schM=view&pbancSn={sn}"
DELAY = 0.3  # seconds between requests (politeness)
MIN_EXPECTED = 50  # K-Startup normally lists 250+ open announcements


def norm_date(s):
    """Normalize date-ish strings to YYYY-MM-DD; return input if not parseable."""
    s = re.sub(r"\s+", " ", htmllib.unescape(s or "")).strip()
    m = re.search(r"(\d{4})[.\-/\s]+(\d{1,2})[.\-/\s]+(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", s)  # 26.07.10
    if m:
        return f"20{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


def make_fetcher():
    """Prefer curl_cffi (Safari TLS fingerprint); fall back to urllib."""
    try:
        from curl_cffi import requests as cr

        sess = cr.Session(impersonate="safari")

        def fetch(url):
            r = sess.get(url, timeout=30)
            return r.status_code, r.text

        return fetch, "curl_cffi"
    except ImportError:
        import urllib.request

        def fetch(url):
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
                    )
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")

        return fetch, "urllib"


def parse_list(html):
    """Extract announcement records from a list page.

    Only the main list (id=bizPbancList) is parsed — the carousel at the top
    repeats featured announcements, so it is discarded. The real list holds
    15 items per page.
    """
    items = []
    parts = html.split('id="bizPbancList"', 1)
    if len(parts) < 2:
        return items
    body = parts[1]
    for blk in re.split(r'<li class="notice">|<li >|<li>', body)[1:]:
        m = re.search(r"go_view\((\d+)\)", blk)
        if not m:
            continue

        def g(pat):
            mm = re.search(pat, blk)
            return re.sub(r"\s+", " ", mm.group(1)).strip() if mm else ""

        lists = [
            re.sub(r"\s+", " ", x).strip()
            for x in re.findall(r'<span class="list"><i[^>]*></i>([^<]+)</span>', blk)
        ]

        def pick(prefix):
            for x in lists:
                if x.startswith(prefix):
                    return x.replace(prefix, "").strip()
            return ""

        items.append(
            {
                "pbancSn": m.group(1),
                "category": htmllib.unescape(
                    g(r'<span class="flag type\d+">\s*([^<]+)</span>')
                ),
                "dday": g(r'<span class="flag day">\s*([^<]+)</span>'),
                "title": htmllib.unescape(g(r'<p class="tit">\s*([^<]+)')),
                "program": htmllib.unescape(lists[0]) if lists else "",
                "org": htmllib.unescape(lists[1]) if len(lists) > 1 else "",
                "start": norm_date(pick("시작일자")),
                "deadline": norm_date(pick("마감일자")),
                "agency_type": g(r'<span class="flag_agency">\s*([^<]+)</span>'),
                "url": DETAIL_URL.format(sn=m.group(1)),
            }
        )
    return items


def cmd_list(args):
    fetch, backend = make_fetcher()
    print(f"[ir-search] fetch backend: {backend}", file=sys.stderr)
    if backend == "urllib":
        print(
            "[ir-search] tip: pip install 'curl_cffi>=0.15' if requests get blocked",
            file=sys.stderr,
        )
    seen = {}
    page = 1
    pages_done = 0
    partial = False  # network/HTTP failure mid-crawl
    no_new_streak = 0
    last_page_had_new = False
    stop_reason = None
    while page <= args.max_pages:
        try:
            status, html = fetch(f"{BASE}?page={page}&schStr=&pbancEndYn=N")
        except Exception as e:  # noqa: BLE001 — fail closed, keep partial data
            code = getattr(e, "code", None)  # urllib raises HTTPError (has .code)
            if code is not None:
                status, html = code, ""
            else:
                print(f"[ir-search] page {page}: network error: {e}", file=sys.stderr)
                partial = True
                stop_reason = "network-error"
                break
        if status != 200:
            print(f"[ir-search] page {page}: HTTP {status} — stopping", file=sys.stderr)
            if status in (403, 412):
                print(
                    "[ir-search] looks blocked; pip install 'curl_cffi>=0.15' and retry.",
                    file=sys.stderr,
                )
            partial = True
            stop_reason = f"http-{status}"
            break
        items = parse_list(html)
        if page == 1 and not items:
            print(
                "ERROR: page 1 parsed 0 items — site structure may have changed",
                file=sys.stderr,
            )
            print(f"[ir-search] {args.output} NOT written (no data)", file=sys.stderr)
            sys.exit(2)
        new = [i for i in items if i["pbancSn"] not in seen]
        for i in items:
            seen[i["pbancSn"]] = i
        pages_done = page
        last_page_had_new = bool(new)
        print(
            f"[ir-search] page {page}: {len(items)} parsed, {len(new)} new, total {len(seen)}",
            file=sys.stderr,
        )
        if not items:
            stop_reason = "reached-total"  # past the last page: empty list
            break
        if not new:
            # past the last page usually only carousel items remain → 0 new,
            # but a single no-new page can also be a transient duplicate page.
            no_new_streak += 1
            if no_new_streak >= 2:
                stop_reason = "no-new-2pages"
                break
        else:
            no_new_streak = 0
        page += 1
        time.sleep(DELAY)
    if stop_reason is None:
        stop_reason = "page-cap"
    print(f"[ir-search] stop reason: {stop_reason} (pages: {pages_done})", file=sys.stderr)

    with open(args.output, "w", encoding="utf-8") as f:
        for i in seen.values():
            f.write(json.dumps(i, ensure_ascii=False) + "\n")
    print(f"[ir-search] saved: {args.output} ({len(seen)} items)", file=sys.stderr)

    fail = False
    if partial:
        print(
            f"WARNING: partial — {pages_done} pages collected "
            f"({len(seen)} items saved, coverage INCOMPLETE)",
            file=sys.stderr,
        )
        fail = True
    if stop_reason == "page-cap" and last_page_had_new:
        print(
            "WARNING: page cap reached — collection may be INCOMPLETE "
            f"(--max-pages {args.max_pages}, last page still had new items)",
            file=sys.stderr,
        )
        fail = True
    if len(seen) < MIN_EXPECTED:
        print(
            f"WARNING: only {len(seen)} items collected (< {MIN_EXPECTED} minimum "
            "expected — K-Startup normally lists 250+ open announcements)",
            file=sys.stderr,
        )
        fail = True
    if fail:
        sys.exit(2)


def strip_html(text):
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", text)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = htmllib.unescape(text)
    return re.sub(r"\n\s*\n+", "\n", text)


def cmd_detail(args):
    fetch, backend = make_fetcher()
    os.makedirs(args.output, exist_ok=True)
    results = []  # (sn, "OK path" | "FAIL reason")
    for sn in args.pbancSn:
        if not sn.isdigit():
            results.append((sn, "FAIL invalid announcement id"))
            print(f"[ir-search] invalid announcement id: {sn}", file=sys.stderr)
            continue
        try:
            status, html = fetch(DETAIL_URL.format(sn=sn))
            if status != 200:
                results.append((sn, f"FAIL HTTP {status}"))
                print(f"[ir-search] {sn}: HTTP {status}", file=sys.stderr)
                continue
            path = os.path.join(args.output, f"{sn}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(strip_html(html))
            results.append((sn, f"OK {path}"))
            print(f"[ir-search] {sn}: saved → {path}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — record failure, keep going
            results.append((sn, f"FAIL {e}"))
            print(f"[ir-search] {sn}: error {e}", file=sys.stderr)
        time.sleep(DELAY)
    failures = [r for r in results if r[1].startswith("FAIL")]
    print(
        f"[ir-search] detail summary: {len(results) - len(failures)} ok, "
        f"{len(failures)} failed",
        file=sys.stderr,
    )
    for sn, res in results:
        print(f"[ir-search]   {sn}: {res}", file=sys.stderr)
    if failures:
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser(description="K-Startup announcement crawler (ir-search)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="collect all currently-recruiting announcements")
    p_list.add_argument("-o", "--output", default="kstartup_all.jsonl")
    p_list.add_argument("--max-pages", type=int, default=40)
    p_list.set_defaults(func=cmd_list)

    p_det = sub.add_parser("detail", help="save detail-page text")
    p_det.add_argument("pbancSn", nargs="+", help="announcement id(s)")
    p_det.add_argument("-o", "--output", default="details")
    p_det.set_defaults(func=cmd_detail)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
