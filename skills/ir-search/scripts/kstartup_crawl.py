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
                "start": pick("시작일자"),
                "deadline": pick("마감일자"),
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
    while page <= args.max_pages:
        status, html = fetch(f"{BASE}?page={page}&schStr=&pbancEndYn=N")
        if status != 200:
            print(f"[ir-search] page {page}: HTTP {status} — stopping", file=sys.stderr)
            if backend == "urllib" and status in (403, 412):
                print(
                    "[ir-search] looks blocked; pip install 'curl_cffi>=0.15' and retry.",
                    file=sys.stderr,
                )
            break
        items = parse_list(html)
        new = [i for i in items if i["pbancSn"] not in seen]
        for i in items:
            seen[i["pbancSn"]] = i
        print(
            f"[ir-search] page {page}: {len(items)} parsed, {len(new)} new, total {len(seen)}",
            file=sys.stderr,
        )
        if not items or not new:
            break  # past the last page only carousel items remain → 0 new
        page += 1
        time.sleep(DELAY)
    with open(args.output, "w", encoding="utf-8") as f:
        for i in seen.values():
            f.write(json.dumps(i, ensure_ascii=False) + "\n")
    print(f"[ir-search] saved: {args.output} ({len(seen)} items)", file=sys.stderr)


def strip_html(text):
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", text)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = htmllib.unescape(text)
    return re.sub(r"\n\s*\n+", "\n", text)


def cmd_detail(args):
    fetch, backend = make_fetcher()
    os.makedirs(args.output, exist_ok=True)
    for sn in args.pbancSn:
        if not sn.isdigit():
            print(f"[ir-search] ignoring invalid announcement id: {sn}", file=sys.stderr)
            continue
        try:
            status, html = fetch(DETAIL_URL.format(sn=sn))
            if status != 200:
                print(f"[ir-search] {sn}: HTTP {status}", file=sys.stderr)
                continue
            path = os.path.join(args.output, f"{sn}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(strip_html(html))
            print(f"[ir-search] {sn}: saved → {path}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — skip failures, keep going
            print(f"[ir-search] {sn}: error {e}", file=sys.stderr)
        time.sleep(DELAY)


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
