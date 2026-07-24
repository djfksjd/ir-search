#!/usr/bin/env python3
"""K-Startup official Open API client (data.go.kr) — bundled with ir-search.

API-first path for the K-Startup source. When a data.go.kr service key is
available, currently-recruiting announcements are fetched from the official
dataset (15125364) instead of crawling the public HTML pages; with no key the
caller falls back to `kstartup_crawl`. The crawl remains the coverage
guarantee — the API is an optimization, so if the API cannot PROVE it covered
the open set it raises ApiError and the caller crawls.

Endpoint (dataset 15125364):
  GET https://apis.data.go.kr/B552735/kisedKstartupService01/getAnnouncementInformation01
      ?serviceKey=<key>&page=N&perPage=M&returnType=json
Modern standard envelope: {"currentCount":..,"data":[{...}],"totalCount":..}.
A legacy {"response":{"body":{"items":{"item":[...]}}}} envelope is also read.

Fail-closed contract (mirrors the crawler):
  - 401/403, or a 200 CAPTCHA/access-denied HTML body -> ManualEscalation
    (the caller exits 3, manual). We do NOT bypass and do NOT silently crawl
    around a block.
  - A structured data.go.kr service-key/quota XML error, an unrecognized
    envelope, a page-cap without proven exhaustion, a below-minimum open set,
    a timeout/transport error -> ApiError (the caller falls back to the crawl,
    the coverage guarantee).

Security: the service key is read from the environment or a local file, is
NEVER printed, and is redacted (with its quote/quote_plus/unquote and
mixed-case %XX variants) from every error message and URL. Redirects are
disabled; the request host and the response host are both validated against
apis.data.go.kr before the body is read.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import attach_download  # noqa: E402 — ManualEscalation + looks_blocked (soft-block)

ORIGIN = "https://apis.data.go.kr"
ENDPOINT = "/B552735/kisedKstartupService01/getAnnouncementInformation01"
ALLOWED_HOST = "apis.data.go.kr"
PER_PAGE = 100
MAX_PAGES = 60  # 60*100 = 6000 records scanned; hard stop
ZERO_OPEN_STOP = 8  # consecutive all-closed pages before declaring exhaustion.
# The dataset is ordered newest-registration-first and OPEN announcements are
# interspersed into the closed tail (measured: open items appear on pages ~1,
# 5, 10, 20, 30 and vanish by ~40). A short streak (e.g. 3) can stop before a
# late reopened/extended announcement; 8 consecutive all-closed pages clears
# the observed tail with margin. This is still a heuristic — the crawl remains
# the exhaustive coverage guarantee (this path falls back to it on any doubt).
DELAY = 0.3  # politeness between page requests (same as the crawler)
TIMEOUT = 20
DETAIL_URL = (
    "https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do?schM=view&pbancSn={sn}"
)
KSTARTUP_HOSTS = ("k-startup.go.kr", "www.k-startup.go.kr")
ENV_VAR = "DATA_GO_KR_KEY"
REDIRECT_STATUSES = (301, 302, 303, 307, 308)


class ApiError(Exception):
    """API path failed; the caller should fall back to the crawler."""


class ApiKeyError(ApiError):
    """The service key was rejected (registration/quota) — surfaced, redacted."""


# ---- key loading (never printed) -------------------------------------------

def _read_env_file(path, name):
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


def load_key():
    """Return the data.go.kr key from env / skill .env / user config, or None.

    Lookup order (first non-empty wins): $DATA_GO_KR_KEY, then a
    `DATA_GO_KR_KEY=...` line in the skill/repo `.env`, then
    ~/.config/ir-search/data_go_kr_key, then the shared
    ~/.config/data_go_kr_key (also read by sole-search). The key is never
    printed.
    """
    v = os.environ.get(ENV_VAR)
    if v and v.strip():
        return v.strip()
    here = os.path.dirname(os.path.abspath(__file__))
    skill_dir = os.path.dirname(here)  # scripts/ -> skill root (has SKILL.md)
    for env_path in (
        os.path.join(skill_dir, ".env"),
        os.path.join(os.path.dirname(skill_dir), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(skill_dir)), ".env"),  # repo root
    ):
        val = _read_env_file(env_path, ENV_VAR)
        if val:
            return val
    for cfg in (
        os.path.expanduser("~/.config/ir-search/data_go_kr_key"),
        os.path.expanduser("~/.config/data_go_kr_key"),  # shared with sole-search
    ):
        try:
            with open(cfg, encoding="utf-8") as f:
                val = f.read().strip()
            if val:
                return val
        except OSError:
            continue
    return None


def has_key():
    return load_key() is not None


def _variant_regex(v):
    """Regex source matching *v* with any %XX escape treated case-insensitively
    (so %2B also matches %2b), without loosening the rest of the key."""
    out = []
    i = 0
    n = len(v)
    while i < n:
        if v[i] == "%" and i + 2 < n and re.fullmatch(r"[0-9A-Fa-f]{2}", v[i + 1 : i + 3]):
            for ch in (v[i + 1], v[i + 2]):
                if ch.isalpha():
                    out.append(f"[{ch.lower()}{ch.upper()}]")
                else:
                    out.append(re.escape(ch))
            out.insert(len(out) - 2, "%")
            i += 3
        else:
            out.append(re.escape(v[i]))
            i += 1
    return "".join(out)


def _make_redactor(key):
    variants = set()
    if key:
        variants.add(key)
        variants.add(urllib.parse.quote(key, safe=""))
        variants.add(urllib.parse.quote_plus(key))
        variants.add(urllib.parse.quote(key, safe="%"))
        try:
            variants.add(urllib.parse.unquote(key))
        except Exception:  # noqa: BLE001 — masking best-effort, never raise
            pass
    variants = sorted((v for v in variants if v), key=len, reverse=True)
    pat = re.compile("|".join(_variant_regex(v) for v in variants)) if variants else None

    def redact(s):
        text = str(s)
        return pat.sub("<KEY>", text) if pat else text

    return redact


# ---- redirect-safe, host-validated fetch -----------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 — disable auto-redirect
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _host_ok(url):
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower().rstrip(".")
    return p.scheme == "https" and host == ALLOWED_HOST


def _encode_key(key):
    """URL-encode the service key safely for both data.go.kr key forms.

    quote(safe="%") preserves the %XX escapes of an already-encoded
    ("Encoding") key while percent-encoding the raw specials of a "Decoding"
    key, and it encodes any control/unsafe character so a malformed key can
    never produce an InvalidURL whose traceback leaks the key.
    """
    return urllib.parse.quote(key, safe="%")


def _fetch_page(key, page, redact, per_page=PER_PAGE):
    url = (
        f"{ORIGIN}{ENDPOINT}?serviceKey={_encode_key(key)}"
        f"&page={page}&perPage={per_page}&returnType=json"
    )
    if not _host_ok(url):
        raise ApiError("refusing to request a non-allowlisted host")
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = _OPENER.open(req, timeout=TIMEOUT)
        status = resp.getcode()
        if status in REDIRECT_STATUSES:
            loc = resp.headers.get("Location", "")
            raise ApiError(f"unexpected redirect to {redact(loc)[:80]}")
        if not _host_ok(resp.geturl()):
            raise ApiError("response came from a non-allowlisted host")
        body = resp.read()
    except attach_download.ManualEscalation:
        raise
    except ApiError:
        raise
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise attach_download.ManualEscalation(
                f"HTTP {e.code} — 차단/인증 신호"
            ) from None
        raise ApiError(f"HTTP {e.code}") from None
    except Exception as e:  # noqa: BLE001 — timeout/IncompleteRead/InvalidURL/... redacted
        raise ApiError(f"request failed: {redact(e)}") from None
    text = body.decode("utf-8", "replace").lstrip("﻿ \t\r\n")
    if text[:1] == "<":
        if re.search(r"OpenAPI_ServiceResponse|returnAuthMsg|returnReasonCode", text):
            msg = re.search(r"<returnAuthMsg>([^<]*)</returnAuthMsg>", text)
            code = re.search(r"<returnReasonCode>([^<]*)</returnReasonCode>", text)
            detail = (msg.group(1) if msg else "") + (
                f" ({code.group(1)})" if code else ""
            )
            raise ApiKeyError(
                f"service key rejected: {redact(detail).strip() or 'XML error envelope'}"
            )
        if attach_download.looks_blocked(text):
            raise attach_download.ManualEscalation("200 위장 차단(CAPTCHA/접근거부) 감지")
        raise ApiError("unexpected non-JSON (HTML) response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ApiError("non-JSON response") from None


# ---- envelope parsing / normalization --------------------------------------

def _raw_container(payload):
    """Return the RAW (unfiltered) record list for any known envelope, or None
    if no recognized container is present. None -> fail closed to the crawl; a
    real empty list -> a possibly-terminal page (validated against totalCount).
    An error field or a legacy non-OK result header yields None (not empty)."""
    if not isinstance(payload, dict):
        return None
    if any(k in payload for k in ("error", "errMsg", "reqErr", "errorMessage")):
        return None
    if isinstance(payload.get("data"), list):
        return payload["data"]
    if isinstance(payload.get("items"), list):
        return payload["items"]
    resp = payload.get("response")
    if isinstance(resp, dict):
        header = resp.get("header")
        if isinstance(header, dict):
            rc = str(header.get("resultCode", "")).strip()
            if rc and rc not in ("00", "0"):
                return None  # legacy error header
        body = resp.get("body")
        if isinstance(body, dict):
            items = body.get("items")
            if isinstance(items, dict):
                item = items.get("item")
                if isinstance(item, list):
                    return item
                if isinstance(item, dict):
                    return [item]
                if item is None:
                    return []
            elif isinstance(items, list):
                return items
    return None


def _records(payload):
    raw = _raw_container(payload)
    return [r for r in raw if isinstance(r, dict)] if raw is not None else []


def _recognized_envelope(payload):
    """True only if *payload* exposes an actual record container (an empty list
    is a legitimate — but total-count-validated — terminal page). An error
    envelope or a counts-only payload is NOT recognized (fails closed)."""
    return _raw_container(payload) is not None


def _total(payload):
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    resp = payload.get("response")
    if isinstance(resp, dict) and isinstance(resp.get("body"), dict):
        candidates.append(resp["body"])
    for src in candidates:
        for k in ("totalCount", "matchCount"):
            v = src.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
    return None


def _pick(rec, keys):
    for k in keys:
        v = rec.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _norm_date(s):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    m = re.search(r"(\d{4})[.\-/\s]+(\d{1,2})[.\-/\s]+(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)  # 20260731
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return s


def _dday(deadline, today=None):
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", deadline or "")
    if not m:
        return ""
    try:
        d = date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return ""
    days = (d - (today or date.today())).days
    return f"D-{days}" if days >= 0 else ""


def _is_open(rec, today=None):
    """Keep currently-recruiting announcements. Conservative: unknown -> keep."""
    prgs = _pick(rec, ("rcrt_prgs_yn", "pbanc_prgs_yn")).upper()
    if prgs == "N":
        return False
    end = _norm_date(_pick(rec, ("pbanc_rcpt_end_dt", "pbanc_end_dt", "rcpt_end_dt")))
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", end)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])) >= (today or date.today())
        except ValueError:
            return True
    return True


def _normalize(rec, today=None):
    """Map an API record to the crawler's jsonl schema (same 10 keys) so the
    two paths are drop-in interchangeable, including diff mode."""
    sn = _pick(rec, ("pbanc_sn", "biz_pbanc_sn", "pbancSn"))
    if not sn:
        return None
    url = _pick(rec, ("detl_pg_url", "pbanc_url", "pblancUrl", "url"))
    if url:
        try:
            parts = urllib.parse.urlsplit(url)
            host = (parts.hostname or "").lower().rstrip(".")
        except ValueError:
            parts, host = None, ""
        if not parts or parts.scheme != "https" or host not in KSTARTUP_HOSTS:
            url = ""
    if not url:
        url = DETAIL_URL.format(sn=urllib.parse.quote(str(sn), safe=""))
    deadline = _norm_date(
        _pick(rec, ("pbanc_rcpt_end_dt", "pbanc_end_dt", "rcpt_end_dt"))
    )
    return {
        "pbancSn": sn,
        "category": _pick(rec, ("supt_biz_clsfc", "intg_pbanc_biz_nm", "biz_enyy")),
        "dday": _dday(deadline, today),
        "title": _pick(rec, ("biz_pbanc_nm", "pbanc_nm", "pbancNm")),
        "program": "",  # not carried by the announcement list envelope
        "org": _pick(rec, ("pbanc_ntrp_nm", "sprv_inst", "biz_prch_dprt_nm")),
        "start": _norm_date(
            _pick(rec, ("pbanc_rcpt_bgng_dt", "rcpt_bgng_dt", "pbanc_bgng_dt"))
        ),
        "deadline": deadline,
        "agency_type": "",  # not carried by the announcement list envelope
        "url": url,
    }


def list_announcements(key, min_expected=1, per_page=PER_PAGE, max_pages=MAX_PAGES):
    """Fetch currently-open announcements via the official API.

    Returns (records, total_count, pages_fetched, proven). *proven* is True
    only when the scan reached a totalCount-consistent end of the dataset;
    False when it stopped on the newest-first zero-open window heuristic (the
    recent window — the caller records this honestly, e.g. stop_reason
    "api-window", and the crawl remains the exhaustive authority). Raises
    ManualEscalation on a block signal, or ApiError on any transport failure,
    envelope/total inconsistency, page-cap without a stop, or below-minimum
    open set — in every ApiError case the caller falls back to the crawl.
    """
    redact = _make_redactor(key)
    out = {}
    total = None
    zero_open_streak = 0
    page = 1
    exhausted = False   # reached a totalCount-consistent end (proven coverage)
    heuristic = False   # stopped on the zero-open window heuristic (recent set)
    while page <= max_pages:
        payload = _fetch_page(key, page, redact, per_page)
        page_total = _total(payload)
        if total is None:
            total = page_total
        elif page_total is not None and page_total != total:
            # a totalCount that shifts mid-scan invalidates the exhaustion proof.
            raise ApiError("totalCount changed mid-scan; using crawl")
        raw = _raw_container(payload)
        if raw is None:
            raise ApiError("unrecognized response envelope; using crawl")
        raw_count = len(raw)
        scanned = (page - 1) * per_page + raw_count
        if raw_count == 0:
            # an empty page is terminal ONLY when totalCount confirms the end.
            if total is not None and (page - 1) * per_page >= total:
                exhausted = True
                break
            raise ApiError("empty page not consistent with totalCount; using crawl")
        recs = [r for r in raw if isinstance(r, dict)]
        if len(recs) != raw_count:
            # a mixed container (valid dicts + junk) is a schema change, and the
            # junk still counts toward totalCount — fail closed to the crawl.
            raise ApiError("mixed unparseable records; using crawl")
        open_on_page = 0
        for r in recs:
            if not _is_open(r):
                continue
            open_on_page += 1  # counted by filter, before dedup/normalize
            norm = _normalize(r)
            if norm is None:
                # an open record whose id field changed cannot be normalized —
                # do not silently drop it while counting it as scanned.
                raise ApiError("open record failed to normalize; using crawl")
            if norm["pbancSn"] not in out:
                out[norm["pbancSn"]] = norm
        if total is not None and scanned >= total:
            exhausted = True
            break
        if raw_count < per_page:
            # a short page that totalCount does not confirm as terminal (or a
            # missing totalCount) is an anomaly — fall back to the crawl.
            raise ApiError("short page not consistent with totalCount; using crawl")
        if open_on_page == 0:
            zero_open_streak += 1
            if zero_open_streak >= ZERO_OPEN_STOP and out:
                heuristic = True
                break
        else:
            zero_open_streak = 0
        page += 1
        time.sleep(DELAY)
    if not exhausted and not heuristic:
        raise ApiError(
            f"page cap ({max_pages}) reached without a stop; using crawl"
        )
    if len(out) < max(1, min_expected):
        raise ApiError(
            f"API open set {len(out)} below expected minimum {min_expected}; "
            "using crawl for full coverage"
        )
    return list(out.values()), total, page, exhausted
