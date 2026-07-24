"""K-Startup official Open API client (data.go.kr) — contract & security tests.

No network: _fetch_page / _OPENER are monkeypatched. Covers key loading &
redaction (incl. mixed-case %XX), host validation, redirect refusal, block
escalation (403 / CAPTCHA), transport-error redaction, both response
envelopes, malformed-envelope fail-closed, pagination coverage (dup pages,
zero-open streak, cap-without-exhaustion), normalization schema parity, and
the cmd_list wiring (success / fallback / manual-escalation).
"""
import types
import urllib.error
from datetime import date

import pytest


# ---- key loading & redaction ----------------------------------------------

@pytest.mark.real_key_loader
def test_load_key_prefers_env(kstartup_api, monkeypatch):
    monkeypatch.setenv("DATA_GO_KR_KEY", "  live-key-123  ")
    assert kstartup_api.load_key() == "live-key-123"


@pytest.mark.real_key_loader
def test_load_key_none_when_absent(kstartup_api, monkeypatch, tmp_path):
    monkeypatch.delenv("DATA_GO_KR_KEY", raising=False)
    monkeypatch.setattr(kstartup_api, "_read_env_file", lambda *a: None)
    monkeypatch.setattr(
        kstartup_api.os.path, "expanduser", lambda p: str(tmp_path / "nope")
    )
    assert kstartup_api.load_key() is None


def test_read_env_file(kstartup_api, tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\nOTHER=x\nDATA_GO_KR_KEY="quoted-key"\n', encoding="utf-8")
    assert kstartup_api._read_env_file(str(p), "DATA_GO_KR_KEY") == "quoted-key"
    assert kstartup_api._read_env_file(str(p), "MISSING") is None


def test_redactor_masks_key_and_encoded_variants(kstartup_api):
    key = "ab+cd/ef=="
    redact = kstartup_api._make_redactor(key)
    enc = kstartup_api.urllib.parse.quote(key, safe="")
    assert key not in redact(f"url?serviceKey={key}&x=1")
    assert enc not in redact(f"url?serviceKey={enc}&x=1")
    assert "<KEY>" in redact(f"boom {key}")


def test_redactor_masks_mixed_case_percent_escapes(kstartup_api):
    key = "ab+cd="  # -> canonical encoding ab%2Bcd%3D
    redact = kstartup_api._make_redactor(key)
    # a server/library that lower-cased the %XX escapes must still be masked
    assert "%2b" not in redact("boom ab%2bcd%3d tail").lower() or "<KEY>" in redact(
        "boom ab%2bcd%3d tail"
    )
    assert "<KEY>" in redact("ab%2bcd%3d")


# ---- key encoding ----------------------------------------------------------

def test_encode_key_passthrough_when_already_encoded(kstartup_api):
    assert kstartup_api._encode_key("AB%2Bcd%3D%3D") == "AB%2Bcd%3D%3D"


def test_encode_key_quotes_raw(kstartup_api):
    out = kstartup_api._encode_key("ab+cd=")
    assert "+" not in out and "=" not in out and "%2B" in out.upper()


def test_encode_key_encodes_control_char(kstartup_api):
    # a malformed key with a control char must be percent-encoded, never
    # allowed to reach urllib as a raw char (which could raise InvalidURL).
    out = kstartup_api._encode_key("ab\ncd")
    assert "\n" not in out and "%0A" in out.upper()


# ---- host validation & redirect refusal ------------------------------------

def test_host_ok(kstartup_api):
    assert kstartup_api._host_ok("https://apis.data.go.kr/B552735/x?serviceKey=k")
    assert not kstartup_api._host_ok("http://apis.data.go.kr/x")  # not https
    assert not kstartup_api._host_ok("https://evil.data.go.kr/x")
    assert not kstartup_api._host_ok("https://apis.data.go.kr.evil.com/x")


class _FakeResp:
    def __init__(self, body=b"", status=200, location=None,
                 url="https://apis.data.go.kr/x"):
        self._body = body
        self._status = status
        self._url = url
        self.headers = {"Location": location} if location else {}

    def getcode(self):
        return self._status

    def geturl(self):
        return self._url

    def read(self):
        return self._body


def test_fetch_page_refuses_redirect(kstartup_api, monkeypatch):
    monkeypatch.setattr(
        kstartup_api._OPENER, "open",
        lambda *a, **k: _FakeResp(status=302, location="https://evil.com/x"),
    )
    with pytest.raises(kstartup_api.ApiError):
        kstartup_api._fetch_page("key", 1, kstartup_api._make_redactor("key"))


def test_fetch_page_refuses_foreign_response_host(kstartup_api, monkeypatch):
    monkeypatch.setattr(
        kstartup_api._OPENER, "open",
        lambda *a, **k: _FakeResp(b"{}", url="https://evil.com/x"),
    )
    with pytest.raises(kstartup_api.ApiError):
        kstartup_api._fetch_page("key", 1, kstartup_api._make_redactor("key"))


def test_fetch_page_403_escalates_manual(kstartup_api, monkeypatch):
    def boom(*a, **k):
        raise urllib.error.HTTPError("https://apis.data.go.kr/x", 403, "Forbidden", {}, None)

    monkeypatch.setattr(kstartup_api._OPENER, "open", boom)
    with pytest.raises(kstartup_api.attach_download.ManualEscalation):
        kstartup_api._fetch_page("key", 1, kstartup_api._make_redactor("key"))


def test_fetch_page_captcha_html_escalates_manual(kstartup_api, monkeypatch):
    body = b"<html><body>access denied - captcha required</body></html>"
    monkeypatch.setattr(kstartup_api._OPENER, "open", lambda *a, **k: _FakeResp(body))
    with pytest.raises(kstartup_api.attach_download.ManualEscalation):
        kstartup_api._fetch_page("key", 1, kstartup_api._make_redactor("key"))


def test_fetch_page_service_key_xml_is_keyerror(kstartup_api, monkeypatch):
    xml = (
        b"<OpenAPI_ServiceResponse><cmmMsgHeader>"
        b"<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
        b"<returnReasonCode>30</returnReasonCode>"
        b"</cmmMsgHeader></OpenAPI_ServiceResponse>"
    )
    monkeypatch.setattr(kstartup_api._OPENER, "open", lambda *a, **k: _FakeResp(xml))
    with pytest.raises(kstartup_api.ApiKeyError) as ei:
        kstartup_api._fetch_page("k", 1, kstartup_api._make_redactor("k"))
    assert "NOT_REGISTERED" in str(ei.value)


def test_fetch_page_transport_error_redacts_key(kstartup_api, monkeypatch):
    key = "secret-key-xyz"

    def boom(*a, **k):
        raise urllib.error.URLError(f"connect failed for serviceKey={key}")

    monkeypatch.setattr(kstartup_api._OPENER, "open", boom)
    with pytest.raises(kstartup_api.ApiError) as ei:
        kstartup_api._fetch_page(key, 1, kstartup_api._make_redactor(key))
    assert key not in str(ei.value)
    assert "<KEY>" in str(ei.value)


# ---- envelope parsing ------------------------------------------------------

def test_records_modern_and_legacy(kstartup_api):
    assert len(kstartup_api._records({"data": [{"pbanc_sn": "1"}, {"pbanc_sn": "2"}]})) == 2
    legacy = {"response": {"body": {"items": {"item": [{"pbanc_sn": "1"}]}}}}
    assert kstartup_api._records(legacy) == [{"pbanc_sn": "1"}]
    single = {"response": {"body": {"items": {"item": {"pbanc_sn": "9"}}}}}
    assert kstartup_api._records(single) == [{"pbanc_sn": "9"}]
    assert kstartup_api._records({}) == []


def test_recognized_envelope(kstartup_api):
    assert kstartup_api._recognized_envelope({"data": []})
    assert kstartup_api._recognized_envelope({"response": {"body": {"items": {}}}})
    # strict: counts alone, an empty body, or an error field are NOT recognized
    assert not kstartup_api._recognized_envelope({"totalCount": 0})
    assert not kstartup_api._recognized_envelope({"response": {"body": {}}})
    assert not kstartup_api._recognized_envelope({"unexpected": "schema"})
    assert not kstartup_api._recognized_envelope({"error": "x", "data": []})
    assert not kstartup_api._recognized_envelope({"reqErr": "bad key"})


def test_total(kstartup_api):
    assert kstartup_api._total({"totalCount": 260}) == 260
    assert kstartup_api._total({"totalCount": "260"}) == 260
    assert kstartup_api._total({"response": {"body": {"totalCount": 5}}}) == 5
    assert kstartup_api._total({}) is None


# ---- date / open filter / normalization ------------------------------------

def test_norm_date(kstartup_api):
    assert kstartup_api._norm_date("20260731") == "2026-07-31"
    assert kstartup_api._norm_date("2026.7.3") == "2026-07-03"
    assert kstartup_api._norm_date("상시") == "상시"


def test_is_open(kstartup_api):
    today = date(2026, 7, 24)
    assert kstartup_api._is_open({"rcrt_prgs_yn": "N"}, today) is False
    assert kstartup_api._is_open({"pbanc_rcpt_end_dt": "20260101"}, today) is False
    assert kstartup_api._is_open({"pbanc_rcpt_end_dt": "20261231"}, today) is True
    assert kstartup_api._is_open({}, today) is True  # unknown -> keep


def test_normalize_matches_crawler_schema(kstartup_api):
    today = date(2026, 7, 24)
    rec = {
        "pbanc_sn": "178481",
        "biz_pbanc_nm": "AI 창업 지원",
        "pbanc_ntrp_nm": "창업진흥원",
        "supt_biz_clsfc": "사업화",
        "pbanc_rcpt_bgng_dt": "20260701",
        "pbanc_rcpt_end_dt": "20260731",
        "detl_pg_url": "https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do?schM=view&pbancSn=178481",
    }
    out = kstartup_api._normalize(rec, today)
    assert out["pbancSn"] == "178481"
    assert out["title"] == "AI 창업 지원"
    assert out["org"] == "창업진흥원"
    assert out["category"] == "사업화"
    assert out["start"] == "2026-07-01"
    assert out["deadline"] == "2026-07-31"
    assert out["dday"] == "D-7"
    assert "k-startup.go.kr" in out["url"]
    # schema parity with the crawler's list record (kstartup_crawl.parse_list)
    assert set(out) == {
        "pbancSn", "category", "dday", "title", "program",
        "org", "start", "deadline", "agency_type", "url",
    }


def test_normalize_drops_foreign_url_and_reconstructs(kstartup_api):
    out = kstartup_api._normalize({"pbanc_sn": "42", "detl_pg_url": "https://evil.com/x"})
    assert out["url"] == kstartup_api.DETAIL_URL.format(sn="42")


def test_normalize_requires_sn(kstartup_api):
    assert kstartup_api._normalize({"biz_pbanc_nm": "no id"}) is None


# ---- list_announcements: pagination, filter, fail-closed -------------------

def _fake_pages(pages):
    def fake(key, page, redact, per_page=None):
        return pages[page - 1] if page - 1 < len(pages) else {"data": []}
    return fake


def _open(i):
    return {"pbanc_sn": str(i), "biz_pbanc_nm": f"n{i}", "pbanc_rcpt_end_dt": "20991231"}


def test_list_collects_open_only(kstartup_api, monkeypatch):
    page1 = {"totalCount": 2, "data": [_open(1), {"pbanc_sn": "2", "rcrt_prgs_yn": "N"}]}
    monkeypatch.setattr(kstartup_api, "_fetch_page", _fake_pages([page1]))
    recs, total, _pages, proven = kstartup_api.list_announcements("key", min_expected=1)
    assert {r["pbancSn"] for r in recs} == {"1"}
    assert total == 2 and proven is True  # scanned 2 >= totalCount 2


def test_list_proven_via_total(kstartup_api, monkeypatch):
    p = kstartup_api.PER_PAGE
    page1 = {"totalCount": 2 * p + 50, "data": [_open(i) for i in range(p)]}
    page2 = {"totalCount": 2 * p + 50, "data": [_open(i) for i in range(p, 2 * p)]}
    page3 = {"totalCount": 2 * p + 50, "data": [_open(i) for i in range(2 * p, 2 * p + 50)]}
    monkeypatch.setattr(kstartup_api, "_fetch_page", _fake_pages([page1, page2, page3]))
    recs, _t, pages, proven = kstartup_api.list_announcements("key", min_expected=1)
    assert len(recs) == 2 * p + 50 and pages == 3 and proven is True


def test_list_empty_page_before_total_raises(kstartup_api, monkeypatch):
    p = kstartup_api.PER_PAGE
    page1 = {"totalCount": 1000, "data": [_open(i) for i in range(p)]}
    page2 = {"totalCount": 1000, "data": []}
    monkeypatch.setattr(kstartup_api, "_fetch_page", _fake_pages([page1, page2]))
    with pytest.raises(kstartup_api.ApiError):
        kstartup_api.list_announcements("key", min_expected=1)


def test_list_truncated_final_page_raises(kstartup_api, monkeypatch):
    p = kstartup_api.PER_PAGE
    page1 = {"totalCount": 2 * p + 50, "data": [_open(i) for i in range(p)]}
    page2 = {"totalCount": 2 * p + 50, "data": [_open(i) for i in range(p, 2 * p)]}
    short = {"totalCount": 2 * p + 50, "data": [_open(i) for i in range(2 * p, 2 * p + 20)]}
    monkeypatch.setattr(kstartup_api, "_fetch_page", _fake_pages([page1, page2, short]))
    with pytest.raises(kstartup_api.ApiError):  # 220 < 250 but page is short
        kstartup_api.list_announcements("key", min_expected=1)


def test_list_below_min_raises(kstartup_api, monkeypatch):
    p = kstartup_api.PER_PAGE
    monkeypatch.setattr(
        kstartup_api, "_fetch_page",
        _fake_pages([{"totalCount": p, "data": [_open(i) for i in range(p)]}]),
    )
    with pytest.raises(kstartup_api.ApiError):  # exhausted p items, below the min
        kstartup_api.list_announcements("key", min_expected=p + 100)


def test_list_duplicate_page_does_not_early_stop(kstartup_api, monkeypatch):
    p = kstartup_api.PER_PAGE
    tot = 3 * p
    page1 = {"totalCount": tot, "data": [_open(i) for i in range(p)]}
    page2 = {"totalCount": tot, "data": [_open(i) for i in range(p)]}          # dup
    page3 = {"totalCount": tot, "data": [_open(i) for i in range(p, 2 * p)]}   # new
    monkeypatch.setattr(
        kstartup_api, "_fetch_page", _fake_pages([page1, page2, page3])
    )
    recs, _t, _pg, _proven = kstartup_api.list_announcements("key", min_expected=1)
    assert len(recs) == 2 * p  # dup page must NOT have stopped collection


def test_list_stops_after_zero_open_streak(kstartup_api, monkeypatch):
    p = kstartup_api.PER_PAGE
    stop = kstartup_api.ZERO_OPEN_STOP
    openpage = {"data": [_open(i) for i in range(p)]}
    closed = {"data": [{"pbanc_sn": f"c{i}", "rcrt_prgs_yn": "N"} for i in range(p)]}
    # one open page, then exactly ZERO_OPEN_STOP all-closed pages -> stop
    monkeypatch.setattr(
        kstartup_api, "_fetch_page", _fake_pages([openpage] + [closed] * stop)
    )
    recs, _t, pages, proven = kstartup_api.list_announcements("key", min_expected=1)
    assert len(recs) == p
    assert pages == 1 + stop  # needed ZERO_OPEN_STOP consecutive all-closed pages
    assert proven is False  # heuristic window stop, NOT a proven exhaustive scan


def test_list_malformed_nonempty_page_raises(kstartup_api, monkeypatch):
    p = kstartup_api.PER_PAGE
    full = {"data": [_open(i) for i in range(p)]}
    # a non-empty data container whose items are all non-dicts -> fail closed
    bad = {"data": ["not", "records"]}
    monkeypatch.setattr(kstartup_api, "_fetch_page", _fake_pages([full, bad]))
    with pytest.raises(kstartup_api.ApiError):
        kstartup_api.list_announcements("key", min_expected=1)


def test_list_cap_without_exhaustion_raises(kstartup_api, monkeypatch):
    p = kstartup_api.PER_PAGE
    full = {"data": [_open(i) for i in range(p)]}
    monkeypatch.setattr(kstartup_api, "_fetch_page", _fake_pages([full, full, full]))
    with pytest.raises(kstartup_api.ApiError):
        kstartup_api.list_announcements("key", min_expected=1, max_pages=2)


def test_list_malformed_later_page_raises(kstartup_api, monkeypatch):
    p = kstartup_api.PER_PAGE
    full = {"data": [_open(i) for i in range(p)]}
    monkeypatch.setattr(
        kstartup_api, "_fetch_page", _fake_pages([full, {"unexpected": "schema"}])
    )
    with pytest.raises(kstartup_api.ApiError):
        kstartup_api.list_announcements("key", min_expected=1)


# ---- cmd_list wiring (API-first) -------------------------------------------

def _args(tmp_path, **kw):
    d = {"output": str(tmp_path / "kstartup.jsonl"), "min_expected": 0, "smoke": False}
    d.update(kw)
    return types.SimpleNamespace(**d)


@pytest.mark.real_key_loader
def test_cmd_list_api_success_writes_manifest(kstartup_crawl, kstartup_api, monkeypatch, tmp_path):
    import json
    monkeypatch.setattr(kstartup_api, "load_key", lambda: "k")
    monkeypatch.setattr(
        kstartup_api, "list_announcements",
        lambda key, min_expected=1, max_pages=None: ([_normalized("178481")], 9566, 3, True),
    )
    args = _args(tmp_path)
    with pytest.raises(SystemExit) as ei:
        kstartup_crawl.cmd_list(args)
    assert ei.value.code == 0
    lines = (tmp_path / "kstartup.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["pbancSn"] == "178481"
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    entry = next(r for r in manifest["runs"] if r["source"] == "kstartup")
    assert entry["stop_reason"] == "api" and entry["exit_code"] == 0
    assert entry["reported_total"] == 9566


@pytest.mark.real_key_loader
def test_cmd_list_api_window_manifest(kstartup_crawl, kstartup_api, monkeypatch, tmp_path):
    import json
    monkeypatch.setattr(kstartup_api, "load_key", lambda: "k")
    # proven=False -> honest recent-window manifest, still exit 0
    monkeypatch.setattr(
        kstartup_api, "list_announcements",
        lambda key, min_expected=1, max_pages=None: ([_normalized("1")], 9566, 8, False),
    )
    with pytest.raises(SystemExit) as ei:
        kstartup_crawl.cmd_list(_args(tmp_path))
    assert ei.value.code == 2  # honest: recent-window is partial coverage
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    entry = next(r for r in manifest["runs"] if r["source"] == "kstartup")
    assert entry["stop_reason"] == "api-window" and entry["status"] == "partial"
    assert any("recent-window" in e for e in entry["errors"])


@pytest.mark.real_key_loader
def test_cmd_list_api_error_falls_back_to_crawl(kstartup_crawl, kstartup_api, monkeypatch, tmp_path):
    monkeypatch.setattr(kstartup_api, "load_key", lambda: "k")

    def raise_api(*a, **k):
        raise kstartup_api.ApiError("wrong key")

    monkeypatch.setattr(kstartup_api, "list_announcements", raise_api)
    # prove control reaches the crawl path (make_fetcher) after the ApiError
    monkeypatch.setattr(
        kstartup_crawl, "make_fetcher",
        lambda: (_ for _ in ()).throw(RuntimeError("CRAWL_REACHED")),
    )
    with pytest.raises(RuntimeError, match="CRAWL_REACHED"):
        kstartup_crawl.cmd_list(_args(tmp_path))


@pytest.mark.real_key_loader
def test_cmd_list_api_block_escalates_exit3(kstartup_crawl, kstartup_api, monkeypatch, tmp_path):
    import json
    monkeypatch.setattr(kstartup_api, "load_key", lambda: "k")

    def raise_block(*a, **k):
        raise kstartup_api.attach_download.ManualEscalation("HTTP 403 — 차단")

    monkeypatch.setattr(kstartup_api, "list_announcements", raise_block)
    called = {"crawl": False}
    monkeypatch.setattr(
        kstartup_crawl, "make_fetcher",
        lambda: called.__setitem__("crawl", True) or (lambda u: (200, ""), "x"),
    )
    with pytest.raises(SystemExit) as ei:
        kstartup_crawl.cmd_list(_args(tmp_path))
    assert ei.value.code == 3
    assert called["crawl"] is False  # a block must NOT fall through to crawling
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    entry = next(r for r in manifest["runs"] if r["source"] == "kstartup")
    assert entry["status"] == "manual" and entry["exit_code"] == 3


def _normalized(sn):
    return {
        "pbancSn": sn, "category": "사업화", "dday": "D-7", "title": "t",
        "program": "", "org": "창업진흥원", "start": "2026-07-01",
        "deadline": "2026-07-31", "agency_type": "",
        "url": f"https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do?schM=view&pbancSn={sn}",
    }
