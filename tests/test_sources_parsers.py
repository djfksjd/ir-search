"""Parser + fail-closed contract tests for sources_crawl.py (no network)."""
import json

import pytest


def fake_fetch(html, status=200):
    def fetch(url, data=None):
        return status, html
    return fetch


# ---------------------------------------------------------------- parsers

def test_bizinfo_parse(sources_crawl, fixture_html):
    items, has_more = sources_crawl.page_bizinfo(fake_fetch(fixture_html("bizinfo_list.html")), 1)
    assert has_more and len(items) == 2
    a = items[0]
    assert a["source"] == "bizinfo"
    assert a["id"] == "PBLN_000000000001"
    assert "합성테스트 스타트업 사업화 지원사업" in a["title"]
    assert a["field"] == "창업"
    assert a["org"] == "중소벤처기업부 / 합성창업진흥원"
    assert (a["apply_start"], a["apply_end"]) == ("2026-07-01", "2026-07-31")
    assert a["reg_date"] == "2026-07-01"
    assert a["url"].startswith("https://www.bizinfo.go.kr/") and "PBLN_000000000001" in a["url"]
    # dotted dates normalize too
    assert (items[1]["apply_start"], items[1]["apply_end"]) == ("2026-07-05", "2026-08-14")


def test_nipa_parse(sources_crawl, fixture_html):
    items, has_more = sources_crawl.page_nipa(fake_fetch(fixture_html("nipa_list.html")), 1)
    assert has_more and len(items) == 2
    a = items[0]
    assert a["source"] == "nipa"
    assert a["id"] == "34567"
    assert a["title"] == "2026년 합성테스트 AI바우처 지원기업 모집 공고"  # comment stripped
    assert a["field"] == "AI바우처"
    assert (a["apply_start"], a["apply_end"]) == ("2026-07-01", "2026-07-20")
    assert a["reg_date"] == "2026-06-30"
    assert a["url"] == "https://www.nipa.kr/home/2-2/34567"


def test_kocca_parse(sources_crawl, fixture_html):
    items, has_more = sources_crawl.page_kocca(fake_fetch(fixture_html("kocca_list.html")), 1)
    assert has_more and len(items) == 2
    a = items[0]
    assert a["source"] == "kocca"
    assert a["id"] == "SYN0000001"
    assert a["field"] == "지원사업"
    assert (a["apply_start"], a["apply_end"]) == ("2026-07-02", "2026-07-30")
    assert a["reg_date"] == "2026-07-02"
    assert a["url"] == "https://www.kocca.kr/kocca/pims/view.do?intcNo=SYN0000001&menuNo=204104"


def test_smtech_parse(sources_crawl, fixture_html):
    items, has_more = sources_crawl.page_smtech(fake_fetch(fixture_html("smtech_list.html")), 1)
    assert has_more and len(items) == 2
    a = items[0]
    assert a["source"] == "smtech"
    assert a["id"] == "S99001"
    assert a["field"] == "디딤돌"
    assert (a["apply_start"], a["apply_end"]) == ("2026-07-01", "2026-07-25")
    assert a["reg_date"] == "2026-06-28"
    assert ";jsessionid" not in a["url"]  # session id must be stripped
    assert a["url"].startswith("https://www.smtech.go.kr/front/ifg/no/notice02_detail.do?")
    assert "ancmId=S99001" in a["url"]


# ------------------------------------------- fail-closed structure change

@pytest.mark.parametrize("source", ["bizinfo", "nipa", "kocca", "smtech"])
def test_structure_change_fails_closed(sources_crawl, source):
    redesigned = "<html><body><div>완전히 바뀐 구조</div></body></html>"
    items, error, stats = sources_crawl.crawl(source, fake_fetch(redesigned), max_pages=3)
    assert items == []
    assert error is not None and "0 items" in error
    assert stats["stop_reason"] == "error"
    # the HTTP response WAS received — the page must be counted as fetched
    assert stats["pages_fetched"] == 1


def test_http_error_fails_closed(sources_crawl):
    items, error, stats = sources_crawl.crawl("bizinfo", fake_fetch("", status=500), max_pages=3)
    assert items == []
    assert "HTTP 500" in error
    assert stats["pages_fetched"] == 0  # no successful response → not counted


def test_main_exit2_and_manifest_on_structure_change(
        sources_crawl, monkeypatch, tmp_path, capsys):
    out = tmp_path / "bizinfo.jsonl"
    monkeypatch.setattr(sources_crawl, "make_fetcher",
                        lambda *a, **k: (fake_fetch("<html>redesign</html>"), "fake"))
    monkeypatch.setattr("sys.argv", ["sources_crawl.py", "list", "bizinfo", "-o", str(out)])
    with pytest.raises(SystemExit) as e:
        sources_crawl.main()
    assert e.value.code == 2
    # jsonl written (empty) and manifest records the partial run
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    (run,) = manifest["runs"]
    assert run["source"] == "bizinfo"
    assert run["status"] == "partial"
    assert run["exit_code"] == 2
    assert run["collected"] == 0
    assert run["pages_fetched"] == 1  # response received, parse failed
    assert run["stop_reason"] == "error"
    assert run["errors"]


def test_main_all_mode_accumulates_manifest(
        sources_crawl, monkeypatch, tmp_path, fixture_html):
    pages = {
        "bizinfo": fixture_html("bizinfo_list.html"),
        "nipa": fixture_html("nipa_list.html"),
        "kocca": fixture_html("kocca_list.html"),
        "smtech": fixture_html("smtech_list.html"),
    }

    def fetch(url, data=None):
        if "bizinfo" in url:
            html = pages["bizinfo"]
        elif "nipa" in url:
            html = pages["nipa"]
        elif "kocca" in url:
            html = pages["kocca"]
        else:
            html = pages["smtech"]
        return 200, html

    out = tmp_path / "sources_all.jsonl"
    monkeypatch.setattr(sources_crawl, "make_fetcher", lambda *a, **k: (fetch, "fake"))
    monkeypatch.setattr("sys.argv",
                        ["sources_crawl.py", "list", "all", "-o", str(out), "--max-pages", "3"])
    sources_crawl.main()  # every source succeeds (dedup stops paging) → exit 0

    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 8  # 2 per source, deduped across repeated pages
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_schema_version"] == 1
    assert [r["source"] for r in manifest["runs"]] == ["bizinfo", "nipa", "kocca", "smtech"]
    for run in manifest["runs"]:
        assert run["status"] == "ok"
        assert run["exit_code"] == 0
        assert run["collected"] == 2
        assert run["pages_fetched"] >= 1
        assert run["cutoff"] is None
        assert run["errors"] == []


# --------------------------------------------------- host_allowed spoofing

def test_host_allowed_accepts_real_sources(sources_crawl):
    assert sources_crawl.host_allowed("https://www.bizinfo.go.kr/sii/x.do?pblancId=1")
    assert sources_crawl.host_allowed("https://www.k-startup.go.kr/web/contents/x.do")
    assert sources_crawl.host_allowed("https://nipa.kr/home/2-2/1")


def test_host_allowed_rejects_evil_prefix_suffix(sources_crawl):
    assert not sources_crawl.host_allowed("https://evilbizinfo.go.kr/x")
    assert not sources_crawl.host_allowed("https://bizinfo.go.kr.evil.example/x")


def test_host_allowed_rejects_userinfo_spoof(sources_crawl):
    assert not sources_crawl.host_allowed("https://bizinfo.go.kr@evil.example/x")
    assert not sources_crawl.host_allowed("https://bizinfo.go.kr:443@evil.example/x")


def test_host_allowed_rejects_plain_http(sources_crawl):
    assert not sources_crawl.host_allowed("http://www.bizinfo.go.kr/x")
    assert not sources_crawl.host_allowed("ftp://www.bizinfo.go.kr/x")
    assert not sources_crawl.host_allowed("not a url")
