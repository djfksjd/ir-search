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
  python3 sources_crawl.py detail <bizinfo-url> -o details/ \
      --download-dir attachments/ [--merge-into bizinfo.jsonl]

Attachment contract (bizinfo/NIPA/KOCCA/SMTECH detail; sole-search port):
  --download-dir downloads every attachment with pre-validated redirects
  (https + per-source allowlist AND robots prefixes checked BEFORE each hop,
  max 5), a 50MB streaming cap, sha256 per file, and a per-announcement
  subdirectory. robots.txt-disallowed paths are recorded as links only
  (skipped_robots). Per-source contracts (verified live 2026-07-24, see
  references/sources.md):
    bizinfo  /cmm/fms/… downloadable; /uploads/… robots-disallowed
    nipa     /comm/getFile?… downloadable (no User-agent:* robots block)
    kocca    popup1 /kocca/noticeFilePop.do → /kocca/noticeFileDown.do
             downloadable; popup2 pms.kocca.kr link-only (skipped_unverified)
    smtech   /front/comn/AtchFileDownload.do?atchFileId=… downloadable
  content_hash is stamped hash v3 (body + sorted attachment sha256s) ONLY
  when every download succeeded; otherwise the body-only v2 hash is kept and
  the record gets attachments_complete:false + exit 2 (partial).

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

Every `list` run (ok or partial) also writes/merges `run_manifest.json`
(schema v1, see run_manifest.py) next to the output jsonl — one run entry
per source, atomically replaced. Coverage reporting reads that file.
"""
import argparse
import html as htmllib
import json
import os
import re
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
from run_manifest import make_run, update_manifest  # noqa: E402
import attach_download  # noqa: E402 — 첨부 다운로드 공용 모듈 (hash v2/v3)

DELAY = 0.4  # seconds between requests (politeness)
MAX_REDIRECTS = 5
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)


def follow_redirects(do_request, url, data=None, allowed_domains=None,
                     robots_disallowed=()):
    """Manually follow redirects, validating EVERY hop against the allowlist.

    Automatic redirect following is disabled in the transports below: a
    permitted host that 302s to an external host must never make us request
    (let alone save) the external response. Each Location is resolved to an
    absolute URL and must pass host_allowed (https + allowlist) — and, when
    *robots_disallowed* is given, attach_download.robots_allowed (robots
    prefix/wildcard rules incl. encoding-disguise checks) — BEFORE any
    request goes out; a violating hop raises, failing that request. At most
    MAX_REDIRECTS hops are followed.

    do_request(url, data) -> (status, text, location-header-or-None).
    """
    import urllib.parse

    current, body = url, data
    # 최초 URL도 리다이렉트 대상과 같은 정책으로 요청 전에 검증한다 —
    # 홉만 검사하면 최초 URL로 차단 대상(pms.kocca.kr, robots 불허 경로)을
    # 직접 넣는 경로가 뚫린다
    if not host_allowed(current, allowed_domains):
        raise RuntimeError(f"initial url not allowed: {current[:80]}")
    if robots_disallowed and not attach_download.robots_allowed(
            current, robots_disallowed):
        raise RuntimeError(f"initial url robots-disallowed: {current[:80]}")
    for _ in range(MAX_REDIRECTS + 1):
        status, text, location = do_request(current, body)
        if status in REDIRECT_STATUSES and location:
            nxt = urllib.parse.urljoin(current, location)
            if not host_allowed(nxt, allowed_domains):
                raise RuntimeError(f"redirect to non-source url blocked: {nxt[:80]}")
            if robots_disallowed and not attach_download.robots_allowed(
                    nxt, robots_disallowed):
                raise RuntimeError(
                    f"redirect to robots-disallowed url blocked: {nxt[:80]}")
            if status == 303 or body is not None:
                body = None  # redirected form submits are re-issued as GET
            current = nxt
            continue
        if status in (401, 403):
            # 1차 페이지·목록·상세 어디서든 차단 신호는 exit 3(수동 전환)로 통일한다 —
            # partial(exit 2)로 강등하지 않는다(Codex ir #4, 양 백엔드 공통).
            raise attach_download.ManualEscalation(f"HTTP {status} — 차단 신호")
        return status, text
    raise RuntimeError(f"redirect chain exceeded {MAX_REDIRECTS} hops")


def make_fetcher(allowed_domains=None, robots_disallowed=()):
    """Prefer curl_cffi (Safari TLS fingerprint); fall back to urllib.

    Both transports have automatic redirects DISABLED; the returned fetch
    follows redirects manually via follow_redirects(), so every hop is
    checked against *allowed_domains* (default: ALLOWED_DOMAINS) and,
    when given, *robots_disallowed* prefixes.
    """
    try:
        from curl_cffi import requests as cr

        sess = cr.Session(impersonate="safari")

        def do_request(url, data=None):
            # data=dict switches to a POST form submit (some boards paginate that way)
            if data is None:
                r = sess.get(url, timeout=30, allow_redirects=False)
            else:
                r = sess.post(url, data=data, timeout=30, allow_redirects=False)
            return r.status_code, r.text, r.headers.get("location")

        backend = "curl_cffi"
    except ImportError:
        import urllib.error
        import urllib.parse
        import urllib.request

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None  # surface 3xx as HTTPError instead of following

        opener = urllib.request.build_opener(_NoRedirect())

        def do_request(url, data=None):
            body = urllib.parse.urlencode(data).encode() if data is not None else None
            req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
            try:
                with opener.open(req, timeout=30) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace"), None
            except urllib.error.HTTPError as e:
                if e.code in REDIRECT_STATUSES:
                    return e.code, "", e.headers.get("Location")
                if e.code in (401, 403):
                    # curl_cffi 백엔드처럼 상태를 반환해 follow_redirects가 차단
                    # 신호(ManualEscalation)로 통일 처리하게 한다(Codex ir #4:
                    # urllib은 raise해서 exit 3 경로를 우회했다).
                    return e.code, "", None
                raise

        backend = "urllib"

    def fetch(url, data=None):
        return follow_redirects(do_request, url, data, allowed_domains,
                                robots_disallowed)

    return fetch, backend


def clean(s):
    return re.sub(r"\s+", " ", htmllib.unescape(s or "")).strip()


def looks_blocked(html):
    """200 위장 소프트 차단(CAPTCHA·접근거부) 감지 — 공용 구현 위임."""
    return attach_download.looks_blocked(html)


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

# Per-source redirect allowlist: a bizinfo list crawl must not be redirected
# even to another *permitted* source's host — each source gets only its own.
# exact-host('=' 접두)로 좁힌다: 서브도메인 와일드카드는 크롤이 kocca.kr →
# pms.kocca.kr(별개 시스템·계약 미확정) 등 다른 서브도메인으로 유도되는 것을
# 허용했다(Codex ir #3). 실제 크롤은 www. 호스트만 쓰므로 www.와 bare만 허용한다.
SOURCE_DOMAINS = {
    "bizinfo": ("=www.bizinfo.go.kr", "=bizinfo.go.kr"),
    "nipa": ("=www.nipa.kr", "=nipa.kr"),
    "kocca": ("=www.kocca.kr", "=kocca.kr"),
    "smtech": ("=www.smtech.go.kr", "=smtech.go.kr"),
}


def crawl(source, fetch, max_pages, smoke=False):
    """Crawl one source. Returns (items, error, stats).

    error is None on full success; otherwise a short reason string. Items
    collected before a mid-crawl failure are always returned (preserved).
    stats is a dict for run_manifest.json: {"pages_fetched", "duplicates",
    "stop_reason"}.
    """
    pager = SOURCES[source]
    seen = {}
    error = None
    no_new_streak = 0
    stop_reason = None
    pages_done = 0
    duplicates = 0
    for page in range(1, max_pages + 1):
        try:
            items, has_more = pager(fetch, page)
        except attach_download.ManualEscalation:
            raise  # 목록 크롤 중 401/403 차단 — partial(2)로 삼키지 않고 수동 전환(3)
        except Exception as e:  # noqa: BLE001 — keep partial data, fail closed
            code = getattr(e, "code", None)  # urllib HTTPError
            error = f"page {page}: " + (f"HTTP {code}" if code is not None else str(e))
            stop_reason = "error"
            break
        # pager() returned → the HTTP response was received; count the page
        # even if it parses to 0 items (fail-closed path below).
        pages_done = page
        if page == 1 and not items:
            error = "page 1 parsed 0 items — site structure may have changed"
            stop_reason = "error"
            break
        new = [i for i in items if i["id"] not in seen]
        duplicates += len(items) - len(new)
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
        if no_new_streak == 0 and not smoke:
            # cap reached while the last page still had new items — more content
            # likely remains. Per the SKILL contract, page-cap == partial (exit 2);
            # intentional recent-mode runs must record this in coverage.
            # --smoke(첫 페이지만 확인하는 저부하 CI)에서는 page-cap coverage 검증만
            # 완화한다 — page-1 파싱 0건·네트워크/HTTP 실패는 그대로 실패(canary).
            error = f"page cap reached at p{max_pages} — collection may be INCOMPLETE"
    print(f"[ir-search] {source}: stop reason: {stop_reason}"
          + (f" ({error})" if error else ""), file=sys.stderr)
    stats = {
        "pages_fetched": pages_done,
        "duplicates": duplicates,
        "stop_reason": stop_reason,
    }
    return list(seen.values()), error, stats


def strip_html(text):
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", text)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = htmllib.unescape(text)
    return re.sub(r"\n\s*\n+", "\n", text)


ALLOWED_DOMAINS = ("bizinfo.go.kr", "nipa.kr", "kocca.kr", "smtech.go.kr", "k-startup.go.kr")


def host_allowed(url, domains=None):
    """Exact-match domain check on the URL's real hostname (https only).

    Uses urlsplit().hostname so userinfo/port tricks ("bizinfo.go.kr:443@evil.example")
    cannot spoof the allowlist — naive string slicing was bypassable. The scheme
    must be https: every allowed source serves https, so a plain-http URL is
    either a typo or a downgrade attempt and is rejected.

    *domains* narrows the allowlist (e.g. per-source redirect checks);
    default is the full ALLOWED_DOMAINS. An entry prefixed with '=' matches
    the EXACT host only (no subdomain wildcard) — e.g. '=www.kocca.kr' does
    not admit pms.kocca.kr (attach_download.host_allowed와 동일 규약).
    """
    import urllib.parse
    if domains is None:
        domains = ALLOWED_DOMAINS
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    for d in domains:
        if d.startswith("="):
            if host == d[1:]:
                return True
        elif host == d or host.endswith("." + d):
            return True
    return False


# ---- bizinfo 첨부 슬라이스 (sole-search 이식, 2026-07-23 실측) ---------------

# robots.txt(2026-07-23 확인)가 /upload·/download 접두를 불허한다 —
# /uploads/ 첨부 링크는 수집하되 다운로드하지 않는다(skipped_robots).
BIZINFO_ROBOTS_DISALLOWED = ("/super", "/upload", "/html", "/images", "/agspa",
                             "/error", "/common", "/lib", "/WEB-INF",
                             "/download", "/direct_do")

# 본문 시작 마커 — 여는 태그만 잡는다 (중첩 div를 regex로 균형 매칭할 수 없으므로)
BIZINFO_START_MARKERS = (r'<div[^>]+class="[^"]*view_cont[^"]*"',
                         r'<div[^>]+id="print_area"')
# 본문 끝 마커 — 시작 마커부터 푸터/다음 주요 섹션까지를 본문으로 자른다
BIZINFO_END_MARKERS = (r'<div[^>]+id="footer"', r'<footer\b',
                       r'<div[^>]+class="[^"]*footer',
                       r'<div[^>]+class="[^"]*btn_area',
                       r'<div[^>]+class="[^"]*paging',
                       r'목록으로|이전글|다음글')


def extract_body(h, start_markers=BIZINFO_START_MARKERS,
                 end_markers=BIZINFO_END_MARKERS):
    """시작 마커 ~ 첫 끝 마커(없으면 문서 끝) 구간의 텍스트. 마커 미발견 시 전체 폴백."""
    sm = None
    for p in start_markers:
        sm = re.search(p, h)
        if sm:
            break
    seg = h[sm.start():] if sm else h
    ends = [m.start() for p in end_markers for m in [re.search(p, seg)] if m]
    if ends:
        seg = seg[:min(ends)]
    return strip_html(seg)


def parse_bizinfo_attachments(h):
    """상세 페이지의 첨부 링크(/cmm/fms/, /uploads/) 목록 — sole-search와 동일 계약."""
    attach = [htmllib.unescape(u) for u in
              re.findall(r'href="(/cmm/fms/[^"]+|/uploads/[^"]+)"', h)]
    seen = []
    for a in attach:
        if a not in seen:
            seen.append(a)
    return [{"url": "https://www.bizinfo.go.kr" + a,
             "filename": a.rsplit("/", 1)[-1].split("?")[0]} for a in seen]


# ---- NIPA·KOCCA·SMTECH 첨부 계약 (2026-07-24 실호출로 확인 — references/sources.md)

# NIPA robots.txt(2026-07-24): `User-agent: *` 블록이 없다(Googlebot 전용
# 규칙 /sea·/tota + Allow:/ 뿐) — 우리 크롤러에 적용되는 불허 경로 없음.
NIPA_ROBOTS_DISALLOWED = ()

# KOCCA robots.txt(2026-07-24) User-agent:* 블록 전사. 첨부 다운로드 엔드포인트
# /kocca/noticeFileDown.do는 "/*/FileDown.do" 패턴(리터럴 "/FileDown.do" 세그먼트
# 필요)과 불일치 — 허용. 와일드카드 패턴은 attach_download._robots_path_match가 처리.
KOCCA_ROBOTS_DISALLOWED = (
    "/kocca/member/", "/kocca/online", "/*/FileDown.do", "/kocca/counsel/",
    "/kocca/bbs/view/userInstRegPage.do", "/kocca/bbs/view/regContent.do",
    "/story/requestResult/", "/story/request/", "/curoms/", "/gamehubpms/",
    "/ourcharacter/", "/bestgame/", "/seriousgame/", "/gameguide/",
    "/broadcastdb.", "/cop/", "/portal/", "/kocca/searchList.do",
    "/kocca/*/list.do", "/kocca/bbs/list/*.do",
)

# SMTECH robots.txt(2026-07-24) User-agent:* 블록 전사(절대 URL 1건은 경로로 변환).
# 첨부 다운로드 엔드포인트 /front/comn/AtchFileDownload.do는 목록에 없음 — 허용.
SMTECH_ROBOTS_DISALLOWED = (
    "/SBA/SA/SbjtAppl_selectSbjtApplList.do",
    "/SBA/SA/HealthMngSbjtRcpt_selectSbjtApplPrg.do",
    "/SBA/ETC/EtcSbjtAppl_forwardEtcSbjtApplList.do",
    "/SBA/ETC/RechSbjtAppl_forwardRechSbjtApplList.do?RECH_ANCM_ID=S20131",
    "/SBA/ETC/RechSbjtAppl_forwardRechSbjtApplList.do?RECH_ANCM_ID=S20132",
    "/SBA/SA/SbjtAppl_selectSbjtPtcpList.do?gMenu=SBA",
    "/SBA/SA/SA_SbjtMbrSprtAncmList.do", "/SBA/SA/SA_SbjtNseeTdpList.do",
    "/sba/ee/ElecEvalSbjtLst.do", "/sba/se/OnlineEval.do",
    "/sba/se/SvorgnSelfEval.do",
    "/SBA/RE/PosnRechEqpm_getPosnRechEqpmMain.do",
    "/SBA/RE/RechEqpmRead_getRechEqpmRead.do",
    "/SBA/RE/BexpPayReq_viewBexpPayReq.do",
    "/SBA/RE/RechEqpmUseSituSv_viewRechEqpmUseSitu.do",
    "/SBA/RE/RechEqpmRevSv_viewRechEqpmRev.do",
    "/SBA/RE/PtcpReqListSv_viewPtcpReqList.do",
    "/SBA/RE/RechEqpmMentor_viewMentorList.do",
    "/SBA/RE/RechEqpmMentorAns_viewMentoringList.do",
    "/SBA/RE/VchrReqSitu_viewVchrReqSitu.do",
    "/SBA/RE/RechEqpmRev_viewRechEqpmRev.do",
    "/SBA/RE/RechEqpmUseSitu_viewRechEqpmUseSitu.do",
    "/SBA/RE/OftnUseEqpm_viewOftnUseEqpm.do",
    "/SBA/RE/PtcpReqList_viewPtcpReqList.do",
    "/SBA/RE/PtcpObjtSbjt_selectPtcpObjtSbjt.do",
    "/SBA/RE/RechEqpmMentoring_viewMentoringList.do",
    "/SBA/EA/AgreAppl_selectAgreWrtSbjt.do",
    "/SBA/EA/StepAgreAppl_selectAgreWrtSbjt.do",
    "/SBA/EA/AgreChng_selectAgreChngObjtSbjt.do",
    "/SBA/EA/ChrgrChng_selectChrgrChngSbjtLst.do",
    "/SBA/EA/MngAdmn_viewMngAdmn.do", "/SBA/EA/AgreRead_selectAgreRead.do",
    "/SBA/RR/PrgrtRptpLst_getPrgrtRptLst.do",
    "/SBA/RR/StepRptpLst_getStepRptLst.do",
    "/SBA/RR/LastRptpLst_getLastRptLst.do",
    "/SBA/RR/MngRsltRptp_viewMngRsltRptp.do",
    "/SBA/RN/RechNoteList_viewRechNoteList.do",
    "/SBA/FA/RechMtrsEstmAdmn.do", "/SBA/FA/KldgPrgtAdmn.do",
    "/SBA/FA/SbjtThesAdmn.do", "/sba/sc/SmbaSancAct.do",
    "/sba/sc/OgovdSancAct.do",
    "/SBA/DR/DemResearch_forwardDemResearchApplMain.do",
    "/SBA/DR/DemResearch_forwardDemResearchApplTeclMain.do",
    "/main/bankLoginGW.do", "/main/bankFrame.do",
    "/OSA/BR/BR_RtrtGuid.do", "/OSA/BR/BR_RtrtSituRead.do",
    "/OSA/BR/BR_PntRtrt.do", "/OSA/BR/BR_CardRtrt.do",
    "/OSA/BR/BR_CardCanRtrt.do", "/OSA/BR/BR_StaxRtrt.do",
    "/OSA/BR/BR_TrstDvlpRtrt.do", "/OSA/PS/PS_PaymPlanRead.do",
    "/OSA/PS/PaymPlanWriteRead.do", "/OSA/OS/OS_CommEvdn.do",
    "/OSA/OS/OS_ExecBrdnRead.do", "/OSA/OS/OS_SetlRpt.do",
    "/OSA/OS/OS_SetlRslt.do", "/csg/qn/qna_list.do",
    "/csg/hi/confirmationInfo.do", "/csg/hi/confirmation.do",
    "/gpin/gPinAuthRequest.do", "/front/nmbi/", "/nmbi/",
    "/csg/cr/crn_list.do", "/csg/id/insusDclr.do",
    "/csg/id/insusDclr_safDclr.do",
)

# 첨부 다운로드 허용 호스트 — **정확한 호스트만**('=' 접두, 실측값). 페이지
# 크롤링(SOURCE_DOMAINS)과 달리 서브도메인 와일드카드를 배제한다: kocca.kr로
# 두면 계약 미확정 pms.kocca.kr로의 리다이렉트가 host 검사를 통과해 버린다.
ATTACH_HOSTS = {
    "bizinfo": ("=www.bizinfo.go.kr", "=bizinfo.go.kr"),
    "nipa": ("=www.nipa.kr", "=nipa.kr"),
    "kocca": ("=www.kocca.kr",),  # pms.kocca.kr 등 서브도메인 배제(계약 미확정)
    "smtech": ("=www.smtech.go.kr", "=smtech.go.kr"),
}

# 첨부 슬라이스 소스별 설정: robots 불허 접두, 공고 id 추출, 본문 마커.
ATTACH_ROBOTS = {
    "bizinfo": BIZINFO_ROBOTS_DISALLOWED,
    "nipa": NIPA_ROBOTS_DISALLOWED,
    "kocca": KOCCA_ROBOTS_DISALLOWED,
    "smtech": SMTECH_ROBOTS_DISALLOWED,
}
ATTACH_ID_PATTERNS = {
    "bizinfo": r"pblancId=(PBLN_\d+)",
    "nipa": r"/home/2-2/(\d+)",
    "kocca": r"intcNo=([A-Za-z0-9]+)",
    "smtech": r"ancmId=([A-Za-z0-9]+)",
}
# 본문 시작/끝 마커 (컨테이너 실측 2026-07-24; bizinfo는 2026-07-23)
BODY_MARKERS = {
    "bizinfo": (BIZINFO_START_MARKERS, BIZINFO_END_MARKERS),
    "nipa": ((r'<div[^>]+class="[^"]*tbWrap[^"]*gonggo[^"]*"',
              r'<div[^>]+class="[^"]*hwp_editor_board_content[^"]*"'),
             (r'<footer\b', r'<div[^>]+id="footer"')),
    "kocca": ((r'<div[^>]+id="contents_body"',),
              (r'<footer\b', r'<div[^>]+id="footer"')),
    "smtech": ((r'<div[^>]+id="subcontent"',),
               (r'<div[^>]+id="footer"', r'<footer\b')),
}


def parse_nipa_attachments(h):
    """NIPA 상세의 첨부(/comm/getFile?...) — 실측 2026-07-24.

    <a href="/comm/getFile?srvcId=...&fileNo=...">파일명.hwp (파일크기: 134 KB)</a>
    앵커 텍스트에서 '(파일크기: …)' 꼬리를 제거해 파일명을 얻는다."""
    out, seen = [], set()
    for m in re.finditer(r'href="(/comm/getFile\?[^"]+)"[^>]*>([\s\S]*?)</a>', h):
        url = "https://www.nipa.kr" + htmllib.unescape(m.group(1))
        if url in seen:
            continue
        seen.add(url)
        name = clean(re.sub(r"<[^>]+>", " ",
                            re.sub(r"\(파일크기[\s\S]*$", "", m.group(2))))
        out.append({"url": url, "filename": name or None})
    return out


def parse_smtech_attachments(h):
    """SMTECH 상세의 첨부 — 실측 2026-07-24.

    <a ... onclick="cfn_AtchFileDownload('<ID>','/front','fileDownFrame')">파일명</a>
    (href="javascript:cfn_..." 변형 포함). 다운로드 URL은 common.js의
    cfn_AtchFileDownloadUrl 계약: <context>/comn/AtchFileDownload.do?atchFileId=<ID>"""
    out, seen = [], set()
    for m in re.finditer(
            r"<a[^>]*cfn_AtchFileDownload\('([0-9A-Fa-f]+)'\s*,\s*'([^']*)'"
            r"[^>]*>([\s\S]*?)</a>", h):
        fid, ctx = m.group(1), m.group(2) or "/front"
        url = f"https://www.smtech.go.kr{ctx}/comn/AtchFileDownload.do?atchFileId={fid}"
        if url in seen:
            continue
        seen.add(url)
        name = clean(re.sub(r"<[^>]+>", " ", m.group(3)))
        out.append({"url": url, "filename": name or None})
    return out


def fetch_kocca_popup(purl):
    """KOCCA 파일 팝업 조회 — attach_download.open_validated 경유:
    각 리다이렉트 홉이 **정확한 호스트(ATTACH_HOSTS) + robots 불허 접두** 검사를
    요청 전에 통과해야 한다. 팝업이 /kocca/bbs/FileDown.do(robots 불허)나
    pms.kocca.kr로 302해도 요청이 나가지 않는다.
    401/403은 다운로드 경로와 동일하게 ManualEscalation(차단 신호 — exit 3)."""
    import urllib.error
    try:
        with attach_download.open_validated(
                purl, ATTACH_HOSTS["kocca"], timeout=30,
                robots_disallowed=KOCCA_ROBOTS_DISALLOWED) as r:
            status = getattr(r, "status", None) or 200
            return status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise attach_download.ManualEscalation(
                f"kocca 파일팝업 HTTP {e.code}") from e
        raise


def _kocca_popup_looks_valid(ph):
    """파일 행 0건인 팝업이 '정상 빈 팝업'인지 판별 — 실측(2026-07-24) 기준
    빈 팝업은 '해당자료가 존재하지 않습니다' 명시 문구를 가진다. **명시 문구만**
    승인한다: '공고관련자료'/board_write01 같은 레이아웃 마커는 Access Denied·
    파일행 마크업 개편 페이지에도 남을 수 있어, 행 0건 + 레이아웃 마커만으로
    '첨부 없음'을 승인하면 fail-open이 된다 — 그 경우는 차단/개편 의심으로
    fail-closed(첨부 유무 불명)."""
    return "해당자료가 존재하지 않습니다" in ph


def parse_kocca_attachments(h, popup_fetch=None):
    """KOCCA 상세의 첨부 — 실측 2026-07-24. 상세 페이지에는 파일이 직접 없고
    팝업 2종을 경유한다:

    1. openNoticeFileList1('<intcNo>') → /kocca/noticeFilePop.do?intcNo=…
       (robots 허용) — 팝업을 추가로 fetch(fetch_kocca_popup: 홉별 정확한
       호스트+robots 검증)해 fn_fileDownload('<intc>','<seq>') 행을 파싱한다.
       다운로드 URL: /kocca/noticeFileDown.do?intcNo=…&seqNo=…
       ("/*/FileDown.do" robots 패턴과 리터럴 불일치 — 허용)
       **팝업 조회 실패는 fail-closed**: 첨부 유무를 알 수 없으므로 해당
       팝업을 download_status "failed" 마커로 기록한다 → complete가 false로
       남아 본문 v2 + attachments_complete:false + exit 2로 강등된다.
    2. openNoticeFileList2('<pblancId>') → pms.kocca.kr(별도 PMS 시스템, JS
       팝업) — 다운로드 계약 미확정: 링크만 기록(download_status
       "skipped_unverified") → attachments_complete는 false로 남는다(fail-closed).
    """
    if popup_fetch is None:
        popup_fetch = fetch_kocca_popup
    out, seen = [], set()
    for intc in dict.fromkeys(re.findall(r"openNoticeFileList1\('([^']+)'\)", h)):
        purl = ("https://www.kocca.kr/kocca/noticeFilePop.do?intcNo="
                + urllib_quote(intc))
        try:
            status, ph = popup_fetch(purl)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
        except attach_download.ManualEscalation:
            raise
        except Exception as e:  # noqa: BLE001 — fail-closed: 첨부 유무 불명
            out.append({"url": purl, "filename": None,
                        "download_status": "failed",
                        "download_reason": f"popup fetch failed: {e}"})
            print(f"WARNING [ir-search] kocca 파일팝업 실패 — 첨부 미확인, "
                  f"partial로 강등: {purl[:70]}: {e}", file=sys.stderr)
            time.sleep(DELAY)
            continue
        rows = list(re.finditer(
            r"<a[^>]*fn_fileDownload\('([^']+)'\s*,\s*'?(\d+)'?\)"
            r"[^>]*>([\s\S]*?)</a>", ph))
        if not rows and not _kocca_popup_looks_valid(ph):
            # 200이지만 예상 구조가 아니다(Access Denied/개편 의심) —
            # 파일 행 0건을 "첨부 없음"으로 오인하면 fail-open이 된다.
            out.append({"url": purl, "filename": None,
                        "download_status": "failed",
                        "download_reason": "popup parsed 0 rows and expected "
                                           "structure markers missing — "
                                           "차단/개편 의심 (fail-closed)"})
            print(f"WARNING [ir-search] kocca 파일팝업 구조 미확인(행 0건) — "
                  f"첨부 미확인, partial로 강등: {purl[:70]}", file=sys.stderr)
            time.sleep(DELAY)
            continue
        for m in rows:
            url = ("https://www.kocca.kr/kocca/noticeFileDown.do?intcNo="
                   f"{urllib_quote(m.group(1))}&seqNo={m.group(2)}")
            if url in seen:
                continue
            seen.add(url)
            out.append({"url": url,
                        "filename": clean(re.sub(r"<[^>]+>", " ", m.group(3)))
                        or None})
        time.sleep(DELAY)
    for pid in dict.fromkeys(re.findall(r"openNoticeFileList2\('([^']+)'\)", h)):
        url = ("https://pms.kocca.kr/pblanc/pblancPopupViewPage.do?pblancId="
               + urllib_quote(pid))
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "filename": None,
                    "download_status": "skipped_unverified",
                    "download_reason": "pms.kocca.kr 팝업 — 다운로드 계약 미확정 "
                                       "(링크만 기록)"})
    return out


def urllib_quote(s):
    import urllib.parse
    return urllib.parse.quote(str(s), safe="")


def collect_attachments(source, h):
    if source == "bizinfo":
        return parse_bizinfo_attachments(h)
    if source == "nipa":
        return parse_nipa_attachments(h)
    if source == "smtech":
        return parse_smtech_attachments(h)
    if source == "kocca":
        # 팝업 조회는 fetch_kocca_popup(open_validated: 홉별 정확한 호스트
        # + robots 검사) 경유 — 페이지 fetch를 재사용하지 않는다.
        return parse_kocca_attachments(h)
    return []


def source_of_url(url):
    for name, domains in SOURCE_DOMAINS.items():
        if host_allowed(url, domains):
            return name
    return None


def make_detail_fetcher():
    """detail 페이지용 fetcher — URL의 소스를 판별해 **첨부와 동일한 호스트
    정책(ATTACH_HOSTS: 정확한 호스트, KOCCA는 =www.kocca.kr만)** + 소스별
    robots 불허 접두(ATTACH_ROBOTS)를 리다이렉트 홉 검사에 적용한다.
    정상 상세 페이지가 pms.kocca.kr나 /kocca/bbs/FileDown.do(robots 불허)로
    302해도 요청이 나가지 않는다. 4개 소스 밖의 허용 URL(K-Startup 등)은
    기존 전체 허용 리스트 fetcher로 폴백한다."""
    fetchers = {}
    shown = [False]

    def get(source):
        if source not in fetchers:
            if source is None:
                f, backend = make_fetcher()
            else:
                f, backend = make_fetcher(ATTACH_HOSTS[source],
                                          ATTACH_ROBOTS[source])
            if not shown[0]:
                print(f"[ir-search] fetch backend: {backend}", file=sys.stderr)
                if backend == "urllib":
                    print("[ir-search] tip: pip install 'curl_cffi>=0.15' "
                          "if requests get blocked", file=sys.stderr)
                shown[0] = True
            fetchers[source] = f
        return fetchers[source]

    def fetch(url, data=None):
        return get(source_of_url(url))(url, data)

    return fetch


def merge_detail(jsonl_path, source, rec_id, content_hash, attachments, complete,
                 hash_version):
    """목록 jsonl의 해당 레코드에 상세 검증 결과를 병합한다 (원자적 교체)."""
    import json as _json
    tmp = jsonl_path + ".tmp"
    found = False
    with open(jsonl_path, encoding="utf-8") as src, \
            open(tmp, "w", encoding="utf-8") as dst:
        for line in src:
            if not line.strip():
                continue
            r = _json.loads(line)
            if r.get("source") == source and str(r.get("id")) == str(rec_id):
                r["content_hash"] = content_hash
                if content_hash is not None:
                    r["hash_version"] = hash_version
                else:
                    r.pop("hash_version", None)  # 해시 없음 = 산식 버전도 무의미
                r["attachments"] = attachments
                r["attachments_complete"] = complete
                found = True
            dst.write(_json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, jsonl_path)
    return found


def cmd_detail(fetch, urls, outdir, download_dir=None, merge_into=None):
    """Save the text of announcement detail pages (any source) for eligibility checks.

    With --download-dir (bizinfo/NIPA/KOCCA/SMTECH detail URLs): also collects
    the attachment links per the source contracts verified live on 2026-07-24
    (see references/sources.md), downloads them under the sole-search security
    contract (pre-validated redirects incl. robots on every hop, 50MB cap,
    sha256, per-announcement subdir) and stamps hash v3 (body + sorted
    attachment sha256s) ONLY when every download succeeded. Any failed/
    blocked/robots-skipped/unverified attachment keeps the body-only v2 hash
    with attachments_complete=false and turns the run into partial (exit 2).
    KOCCA popup-2 attachments (pms.kocca.kr) are link-only
    ("skipped_unverified") until that contract is verified.

    Exit codes: 0 all ok / 2 any URL failed or attachments incomplete /
    3 blocked-signal (attachment or popup HTTP 401/403 → ManualEscalation —
    the record is still merged as v2 + attachments_complete:false first).
    A per-URL success/failure summary goes to stderr.
    """
    import hashlib
    import json as _json
    import os

    os.makedirs(outdir, exist_ok=True)
    results = []  # (url, "OK path" | "FAIL reason" | "PARTIAL reason")
    for url in urls:
        if not host_allowed(url):
            results.append((url, "FAIL non-source host"))
            print(f"[ir-search] skip non-source url: {url[:60]}", file=sys.stderr)
            continue
        try:
            status, h = fetch(url)
            if status in (401, 403):
                # 1차 페이지의 401/403은 차단 신호 — 우회하지 않고 수동 전환(exit 3).
                # 일반 FAIL(exit 2)로 강등하면 partial로 오인된다(Codex ir #4).
                results.append((url, f"FAIL MANUAL HTTP {status} — 차단 신호"))
                print(f"MANUAL [ir-search] {url[:60]}: HTTP {status} 차단 — "
                      "우회하지 않고 수동 확인", file=sys.stderr)
                continue
            if status != 200:
                results.append((url, f"FAIL HTTP {status}"))
                print(f"[ir-search] {url[:60]}: HTTP {status}", file=sys.stderr)
                continue
            if looks_blocked(h):
                # 200으로 위장한 CAPTCHA/접근거부 — 정상 본문이 아니므로 해시/병합
                # 금지, 수동 전환(exit 3). 우회하지 않는다.
                results.append((url, "FAIL MANUAL 200 위장 차단(CAPTCHA/접근거부)"))
                print(f"MANUAL [ir-search] {url[:60]}: 200 위장 차단 감지 — "
                      "우회하지 않고 수동 확인", file=sys.stderr)
                continue
            digest8 = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
            name = re.sub(r"\W+", "_", url.split("://", 1)[1])[:80]
            path = f"{outdir}/{name}_{digest8}.txt"

            source = source_of_url(url)
            if (download_dir or merge_into) and source is None:
                print("[ir-search] NOTE: 이 소스는 첨부 다운로드/병합 미지원 "
                      "(K-Startup은 kstartup_crawl.py detail --download-dir) — "
                      f"본문만 저장: {url[:60]}", file=sys.stderr)

            if source is not None and (download_dir or merge_into):
                m = re.search(ATTACH_ID_PATTERNS[source], url)
                rec_id = m.group(1) if m else None
                manual = None
                try:
                    attachments = collect_attachments(source, h)
                except attach_download.ManualEscalation as e:
                    # 팝업 401/403 등 차단 신호 — 첨부 유무 불명. 병합 없이
                    # 끊으면 재시도 파일의 과거 v3/true가 잔존하므로 failed
                    # 마커로 기록하고 v2/incomplete 병합 후 exit 3.
                    manual = e
                    attachments = [{"url": url, "filename": None,
                                    "download_status": "failed",
                                    "download_reason": f"manual: {e}"}]
                start_markers, end_markers = BODY_MARKERS[source]
                text = extract_body(h, start_markers, end_markers)
                content_hash = hashlib.sha256(text.encode()).hexdigest()
                hash_version = attach_download.HASH_VERSION_BODY
                complete = not attachments  # 링크만 수집: 첨부가 있으면 미검증
                if download_dir and attachments and manual is None:
                    try:
                        attach_hashes = attach_download.process_attachments(
                            attachments, download_dir, DELAY,
                            ATTACH_HOSTS[source], ATTACH_ROBOTS[source],
                            subdir=rec_id or digest8)  # 공고별 폴더 — 동명 충돌 방지
                    except attach_download.ManualEscalation as e:
                        # 401/403 — 우회하지 않는다. 단, 여기서 끊고 나가면
                        # merge가 안 돼 재시도 파일의 과거 v3/complete:true가
                        # 잔존한다 — 본문 v2 + attachments_complete:false를
                        # 반드시 병합하고 partial(exit 2)로 계속한다.
                        manual = e
                        for f in attachments:
                            if "download_status" not in f:
                                f["download_status"] = "failed"
                                f["download_reason"] = f"manual: {e}"
                        complete = False
                    except Exception as e:  # noqa: BLE001
                        # subdir_symlink_blocked 등 process_attachments의 비-Manual
                        # 예외도 바깥 except로 새면 merge를 건너뛰어 과거 v3/complete:true
                        # 레코드가 잔존, 본문 변경이 diff에서 숨는다(Codex ir #2).
                        # 반드시 v2 본문 + attachments_complete:false로 병합하고 partial.
                        for f in attachments:
                            if "download_status" not in f:
                                f["download_status"] = "failed"
                                f["download_reason"] = f"error: {e}"
                        complete = False
                        print(f"WARNING [ir-search] 첨부 처리 오류 — v2/incomplete 병합 "
                              f"후 partial: {e}", file=sys.stderr)
                    else:
                        complete = all(f.get("download_status") == "ok"
                                       for f in attachments)
                        if complete:
                            # hash v3: 본문 + 정렬된 첨부 sha256 — v2와 비교 불가
                            content_hash = attach_download.content_hash_of(
                                text, attach_hashes)
                            hash_version = attach_download.HASH_VERSION_ATTACH
                        # else: 본문만의 v2 해시 유지 — None으로 지우면 반복 실패
                        # 두 런 사이의 본문 변경이 diff에서 숨는다.
                with open(path, "w", encoding="utf-8") as f:
                    f.write(url + "\n")
                    f.write("CONTENT_HASH: " + content_hash + "\n")
                    f.write(f"HASH_VERSION: {hash_version}\n")
                    f.write("ATTACHMENTS: "
                            + _json.dumps(attachments, ensure_ascii=False) + "\n\n")
                    f.write(text)
                if merge_into:
                    if rec_id and not merge_detail(merge_into, source, rec_id,
                                                   content_hash, attachments,
                                                   complete, hash_version):
                        results.append((url, f"FAIL merge: {rec_id} not in "
                                             f"{merge_into}"))
                        print(f"[ir-search] WARNING: {rec_id} 레코드를 "
                              f"{merge_into}에서 못 찾음", file=sys.stderr)
                        time.sleep(DELAY)
                        continue
                if manual is not None:
                    results.append((url, f"FAIL MANUAL {manual}"))
                    print(f"MANUAL [ir-search] 첨부 401/403 — 우회하지 않고 수동 "
                          f"확인으로 전환 (v2/incomplete 병합 완료): {manual}",
                          file=sys.stderr)
                elif not complete:
                    # filename 키가 존재하되 값이 None인 첨부(예: KOCCA popup2
                    # skipped_unverified)를 ', '.join이 TypeError로 터뜨리지
                    # 않도록 None도 "?"로 치환 — .get(k, default)는 키가 없을
                    # 때만 기본값을 주므로 `or "?"`가 필요하다.
                    bad = [f.get("filename") or "?" for f in attachments
                           if f.get("download_status") != "ok"]
                    results.append((url, f"PARTIAL attachments incomplete "
                                         f"({len(bad)}): {', '.join(bad[:5])}"))
                    print(f"WARNING [ir-search] 첨부 {len(bad)}건 실패/생략 — "
                          "hash v2 유지, attachments_complete=false (partial)",
                          file=sys.stderr)
                else:
                    results.append((url, f"OK {path}"))
                print(f"[ir-search] saved: {path}", file=sys.stderr)
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(url + "\n\n" + strip_html(h))
                results.append((url, f"OK {path}"))
                print(f"[ir-search] saved: {path}", file=sys.stderr)
        except attach_download.ManualEscalation as e:
            results.append((url, f"FAIL MANUAL {e}"))
            print(f"MANUAL [ir-search] 첨부 401/403 — 우회하지 않고 수동 확인으로 "
                  f"전환: {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — record failure, keep going
            results.append((url, f"FAIL {e}"))
            print(f"[ir-search] {url[:60]}: error {e}", file=sys.stderr)
        time.sleep(DELAY)
    failures = [r for r in results if r[1].startswith(("FAIL", "PARTIAL"))]
    manuals = [r for r in results if r[1].startswith("FAIL MANUAL")]
    print(
        f"[ir-search] detail summary: {len(results) - len(failures)} ok, "
        f"{len(failures)} failed/partial",
        file=sys.stderr,
    )
    for url, res in results:
        print(f"[ir-search]   {url[:70]}: {res}", file=sys.stderr)
    if manuals:
        sys.exit(3)  # 차단 신호(401/403) — 우회하지 않고 수동 확인으로 전환
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
    p_list.add_argument(
        "--smoke",
        action="store_true",
        help="저부하 CI 스모크: page-cap coverage 검증만 완화(page-1 파싱 0건·"
        "네트워크 실패는 그대로 실패). --max-pages 1 과 함께 첫 페이지 계약만 확인",
    )

    p_det = sub.add_parser("detail", help="save detail-page text for given URLs")
    p_det.add_argument("urls", nargs="+", help="announcement detail URLs")
    p_det.add_argument("-o", "--output", default="details")
    p_det.add_argument(
        "--download-dir",
        help="bizinfo/NIPA/KOCCA/SMTECH 첨부를 이 폴더에(공고별 하위 폴더) "
        "다운로드 — 전부 성공 시에만 hash v3 스탬프, 불완전이면 본문 v2 유지 "
        "+ attachments_complete:false + exit 2",
    )
    p_det.add_argument(
        "--merge-into",
        help="목록 jsonl에 content_hash/hash_version/attachments를 병합",
    )

    args = ap.parse_args()

    if args.cmd == "detail":
        # 소스별 정확한 호스트 + robots 홉 검사 fetcher (첨부와 동일 정책)
        fetch = make_detail_fetcher()
        cmd_detail(fetch, args.urls, args.output,
                   download_dir=args.download_dir, merge_into=args.merge_into)
        return

    names = list(SOURCES) if args.source == "all" else [args.source]
    out = []
    failed = {}  # source -> reason
    blocked = []  # 401/403 차단 신호를 받은 소스 — exit 3
    runs = []  # run_manifest.json entries, one per source
    backend_shown = False
    for name in names:
        # Per-source fetcher: redirects may only stay on that source's host.
        raw_fetch, backend = make_fetcher(SOURCE_DOMAINS[name])

        def fetch(url, data=None, _f=raw_fetch):
            # 목록/상세 200 위장 차단(CAPTCHA)도 수동 전환(exit 3)으로 통일 —
            # parse-failure(partial)로 오인해 blind 재시도하지 않는다(Codex ir #1 후속).
            status, text = _f(url, data)
            if status == 200 and looks_blocked(text):
                raise attach_download.ManualEscalation("200 위장 차단(CAPTCHA/접근거부)")
            return status, text
        if not backend_shown:
            print(f"[ir-search] fetch backend: {backend}", file=sys.stderr)
            if backend == "urllib":
                print(
                    "[ir-search] tip: pip install 'curl_cffi>=0.15' if requests get blocked",
                    file=sys.stderr,
                )
            backend_shown = True
        try:
            items, error, stats = crawl(name, fetch, args.max_pages, smoke=args.smoke)
        except attach_download.ManualEscalation as e:
            # 차단 신호 — 우회하지 않고 수동 전환(exit 3). partial로 강등하지 않는다.
            items, error = [], f"MANUAL {e}"
            stats = {"pages_fetched": 0, "duplicates": 0, "stop_reason": "blocked"}
            blocked.append(name)
        except Exception as e:  # noqa: BLE001 — one source must not sink the rest
            items, error = [], str(e)
            stats = {"pages_fetched": 0, "duplicates": 0, "stop_reason": "error"}
        out.extend(items)  # partial results from a failed source are preserved
        if error:
            failed[name] = error
        is_blocked = name in blocked
        runs.append(make_run(
            name,
            "manual" if is_blocked else ("partial" if error else "ok"),
            3 if is_blocked else (2 if error else 0),
            pages_fetched=stats["pages_fetched"],
            collected=len(items),
            stop_reason=stats["stop_reason"],
            errors=[error] if error else [],
            duplicates=stats["duplicates"],
        ))
        time.sleep(DELAY)
    with open(args.output, "w", encoding="utf-8") as f:
        for i in out:
            f.write(json.dumps(i, ensure_ascii=False) + "\n")
    print(f"[ir-search] saved: {args.output} ({len(out)} items)", file=sys.stderr)
    manifest_path = update_manifest(args.output, runs)
    print(f"[ir-search] manifest: {manifest_path}", file=sys.stderr)
    if failed:
        detail = "; ".join(f"{k}: {v}" for k, v in failed.items())
        print(f"FAILED sources: {detail}", file=sys.stderr)
        if blocked:
            # 차단 신호가 하나라도 있으면 partial이 아니라 수동 전환(exit 3)
            print(f"MANUAL [ir-search] 차단 신호 소스: {', '.join(blocked)} — "
                  "우회하지 않고 수동 확인으로 전환", file=sys.stderr)
            sys.exit(3)
        print(
            "WARNING: coverage INCOMPLETE — treat this run as partial (exit 2)",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
