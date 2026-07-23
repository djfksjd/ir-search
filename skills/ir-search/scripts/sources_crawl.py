#!/usr/bin/env python3
"""Multi-source crawler for Korean government support-program announcements.

Part of the ir-search skill. Covers sources beyond K-Startup
(see kstartup_crawl.py for the K-Startup crawler):

  bizinfo  Bizinfo (기업마당) — largest aggregated portal, all ministries/regions
  nipa     NIPA (정보통신산업진흥원) — AI/ICT programs
  kocca    KOCCA (한국콘텐츠진흥원) — content-industry programs
  smtech   SMTECH (중소기업 기술개발사업) — SME R&D calls

Only public announcement pages are accessed; no login, no private areas.
A polite delay is applied between requests.

Usage:
  python3 sources_crawl.py list bizinfo -o bizinfo.jsonl --max-pages 20
  python3 sources_crawl.py list all -o all_sources.jsonl
  python3 sources_crawl.py detail <url> [<url> ...] -o details/

Unified JSONL schema:
  {"source", "id", "title", "field", "org", "apply_start", "apply_end",
   "reg_date", "url"}

Dependency: curl_cffi>=0.15 recommended (TLS-fingerprint friendly).
Falls back to urllib; if blocked, an install hint is printed.

Exit codes (fail-closed contract):
  0  every requested source crawled successfully (detail: every URL saved)
  2  partial / failure — at least one source failed (HTTP/network error,
     first page parsed 0 items = probable site redesign) or, in detail mode,
     at least one URL failed. Whatever was collected IS still written to the
     output file; exit 2 means coverage is incomplete, not that the file is
     empty. Callers MUST NOT treat exit 2 as a clean success.
"""
import argparse
import html as htmllib
import json
import re
import sys
import time

DELAY = 0.4  # seconds between requests (politeness)


def make_fetcher():
    """Prefer curl_cffi (Safari TLS fingerprint); fall back to urllib."""
    try:
        from curl_cffi import requests as cr

        sess = cr.Session(impersonate="safari")

        def fetch(url, data=None):
            # data=dict switches to a POST form submit (some boards paginate that way)
            if data is None:
                r = sess.get(url, timeout=30)
            else:
                r = sess.post(url, data=data, timeout=30)
            return r.status_code, r.text

        return fetch, "curl_cffi"
    except ImportError:
        import urllib.parse
        import urllib.request

        def fetch(url, data=None):
            body = urllib.parse.urlencode(data).encode() if data is not None else None
            req = urllib.request.Request(
                url,
                data=body,
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


def clean(s):
    return re.sub(r"\s+", " ", htmllib.unescape(s or "")).strip()


def norm_date(s):
    """Normalize date-ish strings to YYYY-MM-DD; return input if not parseable."""
    s = clean(s)
    m = re.search(r"(\d{4})[.\-/\s]+(\d{1,2})[.\-/\s]+(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", s)  # 26.07.10
    if m:
        return f"20{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


def split_period(s):
    """Split '2026-07-07 ~ 2026-07-17'-style ranges into (start, end)."""
    parts = re.split(r"~|∼|～", s)  # ASCII tilde, U+223C, U+FF5E (fullwidth)
    if len(parts) == 2:
        return norm_date(parts[0]), norm_date(parts[1])
    return "", norm_date(s)


# --------------------------------------------------------------------------
# Per-source parsers. Each returns (items, has_more) for one page.
# Structures verified live on 2026-07-11; if a site redesign breaks a parser,
# it fails loudly (0 items) rather than returning wrong data.
# --------------------------------------------------------------------------

def page_bizinfo(fetch, page):
    # List rows: no / field / title+link(pblancId) / period / ministry / agency / reg / views
    url = (
        "https://www.bizinfo.go.kr/sii/siia/selectSIIA200View.do"
        f"?rows=15&cpage={page}&schEndAt=N"
    )
    status, h = fetch(url)
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    items = []
    for row in re.findall(r"<tr>[\s\S]*?</tr>", h):
        m = re.search(r'href\s*=\s*"([^"]*pblancId=(PBLN_\d+)[^"]*)"[^>]*>\s*([\s\S]*?)</a>', row)
        if not m:
            continue
        tds = [clean(re.sub(r"<[^>]+>", " ", td)) for td in re.findall(r"<td[^>]*>([\s\S]*?)</td>", row)]
        # tds: [no, field, title-cell, period, ministry, agency, reg_date, views]
        start, end = split_period(tds[3]) if len(tds) > 3 else ("", "")
        items.append(
            {
                "source": "bizinfo",
                "id": m.group(2),
                "title": clean(m.group(3)),
                "field": tds[1] if len(tds) > 1 else "",
                "org": " / ".join(x for x in tds[4:6] if x) if len(tds) > 5 else "",
                "apply_start": start,
                "apply_end": end,
                "reg_date": tds[6] if len(tds) > 6 else "",
                "url": f"https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId={m.group(2)}",
            }
        )
    return items, bool(items)


def page_nipa(fetch, page):
    # Table rows: no / D-day / title+link(/home/2-2/{id}) + program tag + period / author / reg
    status, h = fetch(f"https://www.nipa.kr/home/2-2?curPage={page}")
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    items = []
    for row in re.findall(r"<tr>[\s\S]*?</tr>", h):
        m = re.search(r'href="(/home/2-2/(\d+))"[^>]*>([\s\S]*?)</a>', row)
        if not m:
            continue
        period = re.search(r"신청기간\s*:\s*([^<]+)", row)
        start, end = split_period(period.group(1)) if period else ("", "")
        prog = re.search(r'<span class="box[^"]*">([^<]+)</span>', row)
        reg = re.findall(r'<span class="bco">\s*(\d{4}-\d{2}-\d{2})\s*</span>', row)
        items.append(
            {
                "source": "nipa",
                "id": m.group(2),
                "title": clean(re.sub(r"<!--[\s\S]*?-->", "", m.group(3))),
                "field": clean(prog.group(1)) if prog else "",
                "org": "NIPA",
                "apply_start": start,
                "apply_end": end,
                "reg_date": reg[-1] if reg else "",
                "url": f"https://www.nipa.kr{m.group(1)}",
            }
        )
    return items, bool(items)


def page_kocca(fetch, page):
    # Rows: category / title+link(view.do?intcNo=...) / notice date / apply period / views
    # Pagination is a POST form submit (fn_egov_select_linkPage), not a GET param.
    status, h = fetch(
        "https://www.kocca.kr/kocca/pims/list.do",
        data={"menuNo": "204104", "pageIndex": str(page)},
    )
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    items = []
    for row in re.findall(r"<tr>[\s\S]*?</tr>", h):
        m = re.search(r'href="(/kocca/pims/view\.do\?intcNo=([^&"]+)[^"]*)"[^>]*>([\s\S]*?)</a>', row)
        if not m:
            continue
        cat = re.search(r'<span class="category_color\d+">([^<]+)</span>', row)
        period = re.search(r'data-label="접수기간">\s*([^<]+)', row)
        notice = re.search(r'data-label="공고일">\s*([^<]+)', row)
        start, end = split_period(period.group(1)) if period else ("", "")
        items.append(
            {
                "source": "kocca",
                "id": m.group(2),
                "title": clean(m.group(3)),
                "field": clean(cat.group(1)) if cat else "",
                "org": "KOCCA",
                "apply_start": start,
                "apply_end": end,
                "reg_date": norm_date(notice.group(1)) if notice else "",
                "url": "https://www.kocca.kr" + htmllib.unescape(m.group(1)),
            }
        )
    return items, bool(items)


def page_smtech(fetch, page):
    # Rows: program / title+link(notice02_detail.do?...ancmId=...) / period / reg / status icons
    status, h = fetch(
        f"https://www.smtech.go.kr/front/ifg/no/notice02_list.do?pageIndex={page}"
    )
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    items = []
    for row in re.findall(r"<tr>[\s\S]*?</tr>", h):
        m = re.search(r'href="(/front/ifg/no/notice02_detail\.do[^"]*ancmId=([^&"]+)[^"]*)"[^>]*>([\s\S]*?)</a>', row)
        if not m:
            continue
        tds = [clean(re.sub(r"<[^>]+>", " ", td)) for td in re.findall(r"<td[^>]*>([\s\S]*?)</td>", row)]
        period = next((t for t in tds if "~" in t), "")
        start, end = split_period(period) if period else ("", "")
        reg = next((t for t in tds if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t)), "")
        # Drop the session id from the path; keep only the stable query params.
        path = re.sub(r";jsessionid=[^?]*", "", htmllib.unescape(m.group(1)))
        items.append(
            {
                "source": "smtech",
                "id": m.group(2),
                "title": clean(m.group(3)),
                "field": tds[1] if len(tds) > 1 else "",
                "org": "SMTECH(중소기업기술정보진흥원)",
                "apply_start": start,
                "apply_end": end,
                "reg_date": reg,
                "url": f"https://www.smtech.go.kr{path}",
            }
        )
    return items, bool(items)


SOURCES = {
    "bizinfo": page_bizinfo,
    "nipa": page_nipa,
    "kocca": page_kocca,
    "smtech": page_smtech,
}


def crawl(source, fetch, max_pages):
    """Crawl one source. Returns (items, error).

    error is None on full success; otherwise a short reason string. Items
    collected before a mid-crawl failure are always returned (preserved).
    """
    pager = SOURCES[source]
    seen = {}
    error = None
    no_new_streak = 0
    stop_reason = None
    for page in range(1, max_pages + 1):
        try:
            items, has_more = pager(fetch, page)
        except Exception as e:  # noqa: BLE001 — keep partial data, fail closed
            code = getattr(e, "code", None)  # urllib HTTPError
            error = f"page {page}: " + (f"HTTP {code}" if code is not None else str(e))
            stop_reason = "error"
            break
        if page == 1 and not items:
            error = "page 1 parsed 0 items — site structure may have changed"
            stop_reason = "error"
            break
        new = [i for i in items if i["id"] not in seen]
        for i in items:
            seen[i["id"]] = i
        print(
            f"[ir-search] {source} p{page}: {len(items)} parsed, {len(new)} new, total {len(seen)}",
            file=sys.stderr,
        )
        if not has_more or not items:
            stop_reason = "reached-total"
            break
        if not new:
            # one no-new page can be a transient duplicate; require two in a row
            no_new_streak += 1
            if no_new_streak >= 2:
                stop_reason = "no-new-2pages"
                break
        else:
            no_new_streak = 0
        time.sleep(DELAY)
    if stop_reason is None:
        stop_reason = "page-cap"
        if no_new_streak == 0:
            # cap reached while the last page still had new items — more content
            # likely remains. Per the SKILL contract, page-cap == partial (exit 2);
            # intentional recent-mode runs must record this in coverage.
            error = f"page cap reached at p{max_pages} — collection may be INCOMPLETE"
    print(f"[ir-search] {source}: stop reason: {stop_reason}"
          + (f" ({error})" if error else ""), file=sys.stderr)
    return list(seen.values()), error


def strip_html(text):
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", text)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = htmllib.unescape(text)
    return re.sub(r"\n\s*\n+", "\n", text)


ALLOWED_DOMAINS = ("bizinfo.go.kr", "nipa.kr", "kocca.kr", "smtech.go.kr", "k-startup.go.kr")


def host_allowed(url):
    """Exact-match domain check on the URL's real hostname.

    Uses urlsplit().hostname so userinfo/port tricks ("bizinfo.go.kr:443@evil.example")
    cannot spoof the allowlist — naive string slicing was bypassable.
    """
    import urllib.parse
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def cmd_detail(fetch, urls, outdir):
    """Save the text of announcement detail pages (any source) for eligibility checks.

    Exits 2 if any URL fails; a per-URL success/failure summary goes to stderr.
    """
    import hashlib
    import os

    os.makedirs(outdir, exist_ok=True)
    results = []  # (url, "OK path" | "FAIL reason")
    for url in urls:
        if not host_allowed(url):
            results.append((url, "FAIL non-source host"))
            print(f"[ir-search] skip non-source url: {url[:60]}", file=sys.stderr)
            continue
        try:
            status, h = fetch(url)
            if status != 200:
                results.append((url, f"FAIL HTTP {status}"))
                print(f"[ir-search] {url[:60]}: HTTP {status}", file=sys.stderr)
                continue
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
            name = re.sub(r"\W+", "_", url.split("://", 1)[1])[:80]
            path = f"{outdir}/{name}_{digest}.txt"
            with open(path, "w", encoding="utf-8") as f:
                f.write(url + "\n\n" + strip_html(h))
            results.append((url, f"OK {path}"))
            print(f"[ir-search] saved: {path}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — record failure, keep going
            results.append((url, f"FAIL {e}"))
            print(f"[ir-search] {url[:60]}: error {e}", file=sys.stderr)
        time.sleep(DELAY)
    failures = [r for r in results if r[1].startswith("FAIL")]
    print(
        f"[ir-search] detail summary: {len(results) - len(failures)} ok, "
        f"{len(failures)} failed",
        file=sys.stderr,
    )
    for url, res in results:
        print(f"[ir-search]   {url[:70]}: {res}", file=sys.stderr)
    if failures:
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser(
        description="Crawl Korean support-program announcement boards (ir-search)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="crawl announcement lists")
    p_list.add_argument("source", choices=[*SOURCES, "all"], help="source to crawl")
    p_list.add_argument("-o", "--output", default="sources.jsonl")
    p_list.add_argument(
        "--max-pages",
        type=int,
        default=30,
        help="page cap per source (bizinfo lists many announcements — "
        "recent pages usually suffice)",
    )

    p_det = sub.add_parser("detail", help="save detail-page text for given URLs")
    p_det.add_argument("urls", nargs="+", help="announcement detail URLs")
    p_det.add_argument("-o", "--output", default="details")

    args = ap.parse_args()

    fetch, backend = make_fetcher()
    print(f"[ir-search] fetch backend: {backend}", file=sys.stderr)
    if backend == "urllib":
        print(
            "[ir-search] tip: pip install 'curl_cffi>=0.15' if requests get blocked",
            file=sys.stderr,
        )

    if args.cmd == "detail":
        cmd_detail(fetch, args.urls, args.output)
        return

    names = list(SOURCES) if args.source == "all" else [args.source]
    out = []
    failed = {}  # source -> reason
    for name in names:
        try:
            items, error = crawl(name, fetch, args.max_pages)
        except Exception as e:  # noqa: BLE001 — one source must not sink the rest
            items, error = [], str(e)
        out.extend(items)  # partial results from a failed source are preserved
        if error:
            failed[name] = error
        time.sleep(DELAY)
    with open(args.output, "w", encoding="utf-8") as f:
        for i in out:
            f.write(json.dumps(i, ensure_ascii=False) + "\n")
    print(f"[ir-search] saved: {args.output} ({len(out)} items)", file=sys.stderr)
    if failed:
        detail = "; ".join(f"{k}: {v}" for k, v in failed.items())
        print(f"FAILED sources: {detail}", file=sys.stderr)
        print(
            "WARNING: coverage INCOMPLETE — treat this run as partial (exit 2)",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
