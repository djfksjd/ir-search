"""Parser + contract tests for kstartup_crawl.py (no network).

Note: K-Startup list pages are HTML (not JSON) — the fixture mirrors the
HTML structure parse_list() consumes, including the top carousel that must
be excluded.
"""
import json
import types

import pytest


def make_args(tmp_path, **kw):
    return types.SimpleNamespace(
        output=str(tmp_path / kw.pop("output", "kstartup_all.jsonl")),
        max_pages=kw.pop("max_pages", 5),
        min_expected=kw.pop("min_expected", 0),
    )


def test_parse_list_fields_and_carousel_exclusion(kstartup_crawl, fixture_html):
    items = kstartup_crawl.parse_list(fixture_html("kstartup_list.html"))
    assert len(items) == 2  # carousel entries (900001 repeat, 777777) ignored
    sns = {i["pbancSn"] for i in items}
    assert sns == {"900001", "900002"}
    assert "777777" not in sns  # carousel-only item must not appear
    a = next(i for i in items if i["pbancSn"] == "900001")
    assert a["title"] == "2026년 합성 예비창업패키지 예비창업자 모집 공고"
    assert a["category"] == "사업화"
    assert a["dday"] == "D-7"
    assert a["program"] == "합성 창업지원 프로그램"
    assert a["org"] == "합성창업진흥원"
    assert (a["start"], a["deadline"]) == ("2026-07-01", "2026-07-31")
    assert a["agency_type"] == "공공기관"
    assert a["url"].endswith("pbancSn=900001")
    b = next(i for i in items if i["pbancSn"] == "900002")
    assert (b["start"], b["deadline"]) == ("2026-07-05", "2026-08-07")  # dotted dates


def test_cmd_list_dedup_and_manifest_ok(kstartup_crawl, monkeypatch, tmp_path, fixture_html):
    html = fixture_html("kstartup_list.html")
    calls = []

    def fetch(url):
        calls.append(url)
        return 200, html  # every page identical → pages 2,3 are all duplicates

    monkeypatch.setattr(kstartup_crawl, "make_fetcher", lambda: (fetch, "fake"))
    args = make_args(tmp_path)
    kstartup_crawl.cmd_list(args)  # must exit 0 (no SystemExit)

    lines = [json.loads(x) for x in
             (tmp_path / "kstartup_all.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2  # deduped by pbancSn across pages
    assert len(calls) == 3  # p1 new, p2 no-new, p3 no-new → stop

    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_schema_version"] == 1
    (run,) = manifest["runs"]
    assert run["source"] == "kstartup"
    assert run["status"] == "ok"
    assert run["exit_code"] == 0
    assert run["pages_fetched"] == 3
    assert run["collected"] == 2
    assert run["duplicates"] == 4  # 2 dup items on each of pages 2 and 3
    assert run["stop_reason"] == "no-new-2pages"
    assert run["errors"] == []


def test_cmd_list_structure_change_exit2_no_jsonl(kstartup_crawl, monkeypatch, tmp_path):
    monkeypatch.setattr(kstartup_crawl, "make_fetcher",
                        lambda: (lambda url: (200, "<html>redesign</html>"), "fake"))
    args = make_args(tmp_path)
    with pytest.raises(SystemExit) as e:
        kstartup_crawl.cmd_list(args)
    assert e.value.code == 2
    assert not (tmp_path / "kstartup_all.jsonl").exists()  # no data → no file
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    (run,) = manifest["runs"]
    assert run["status"] == "partial"
    assert run["stop_reason"] == "parse-failure"
    assert run["collected"] == 0
    assert run["pages_fetched"] == 1  # HTTP 200 was received; page counts as fetched
    assert run["errors"]


def test_cmd_list_min_expected_partial(kstartup_crawl, monkeypatch, tmp_path, fixture_html):
    html = fixture_html("kstartup_list.html")
    monkeypatch.setattr(kstartup_crawl, "make_fetcher",
                        lambda: (lambda url: (200, html), "fake"))
    args = make_args(tmp_path, min_expected=50)  # only 2 items → suspicious
    with pytest.raises(SystemExit) as e:
        kstartup_crawl.cmd_list(args)
    assert e.value.code == 2
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    (run,) = manifest["runs"]
    assert run["status"] == "partial"
    assert run["exit_code"] == 2
    assert run["collected"] == 2
    assert any("minimum expected" in err for err in run["errors"])


def test_cmd_list_network_error_partial(kstartup_crawl, monkeypatch, tmp_path, fixture_html):
    html = fixture_html("kstartup_list.html")
    state = {"n": 0}

    def fetch(url):
        state["n"] += 1
        if state["n"] == 1:
            return 200, html
        raise OSError("connection reset")

    monkeypatch.setattr(kstartup_crawl, "make_fetcher", lambda: (fetch, "fake"))
    args = make_args(tmp_path)
    with pytest.raises(SystemExit) as e:
        kstartup_crawl.cmd_list(args)
    assert e.value.code == 2
    # partial jsonl still saved
    lines = (tmp_path / "kstartup_all.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    (run,) = manifest["runs"]
    assert run["status"] == "partial"
    assert run["stop_reason"] == "network-error"
    assert run["pages_fetched"] == 1
