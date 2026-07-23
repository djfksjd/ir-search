"""Redirect-boundary tests (no network).

The fetch layer must not auto-follow redirects: every Location is resolved
to an absolute URL and checked (https + allowlist) BEFORE any request goes
out. An allowed host 302-ing to an external host must fail the request
without the external host ever being contacted.
"""
import pytest


def make_transport(script):
    """Fake do_request keyed by URL; records every URL actually requested.

    script: {url: (status, text, location)}
    """
    requested = []

    def do_request(url, data=None):
        requested.append(url)
        if url not in script:
            raise AssertionError(f"unexpected request to {url}")
        return script[url]

    return do_request, requested


# ------------------------------------------------------- sources_crawl.py

def test_redirect_to_external_host_blocked_and_never_requested(sources_crawl):
    do_request, requested = make_transport({
        "https://www.bizinfo.go.kr/a.do": (302, "", "https://evil.example/steal"),
    })
    with pytest.raises(RuntimeError, match="redirect to non-source url blocked"):
        sources_crawl.follow_redirects(do_request, "https://www.bizinfo.go.kr/a.do")
    # (a) the external URL must never be requested; (b) the request failed
    assert requested == ["https://www.bizinfo.go.kr/a.do"]


def test_redirect_chain_allowed_to_external_blocked(sources_crawl):
    """allowed → allowed → external: fails at the external hop only."""
    do_request, requested = make_transport({
        "https://bizinfo.go.kr/a": (301, "", "https://www.bizinfo.go.kr/b"),
        "https://www.bizinfo.go.kr/b": (302, "", "https://attacker.example/c"),
    })
    with pytest.raises(RuntimeError, match="blocked"):
        sources_crawl.follow_redirects(do_request, "https://bizinfo.go.kr/a")
    assert requested == ["https://bizinfo.go.kr/a", "https://www.bizinfo.go.kr/b"]
    assert not any("attacker.example" in u for u in requested)


def test_redirect_allowed_www_variant_chain_succeeds(sources_crawl):
    do_request, requested = make_transport({
        "https://bizinfo.go.kr/a": (302, "", "https://www.bizinfo.go.kr/a"),
        "https://www.bizinfo.go.kr/a": (200, "<html>ok</html>", None),
    })
    status, text = sources_crawl.follow_redirects(do_request, "https://bizinfo.go.kr/a")
    assert (status, text) == (200, "<html>ok</html>")
    assert requested == ["https://bizinfo.go.kr/a", "https://www.bizinfo.go.kr/a"]


def test_redirect_relative_location_resolved_and_followed(sources_crawl):
    do_request, requested = make_transport({
        "https://www.bizinfo.go.kr/a/b.do": (302, "", "/c/d.do"),
        "https://www.bizinfo.go.kr/c/d.do": (200, "ok", None),
    })
    status, text = sources_crawl.follow_redirects(
        do_request, "https://www.bizinfo.go.kr/a/b.do")
    assert (status, text) == (200, "ok")
    assert requested[-1] == "https://www.bizinfo.go.kr/c/d.do"


def test_redirect_http_downgrade_blocked(sources_crawl):
    """Even to an allowed host, a redirect down to plain http is refused."""
    do_request, requested = make_transport({
        "https://www.bizinfo.go.kr/a": (302, "", "http://www.bizinfo.go.kr/a"),
    })
    with pytest.raises(RuntimeError, match="blocked"):
        sources_crawl.follow_redirects(do_request, "https://www.bizinfo.go.kr/a")
    assert requested == ["https://www.bizinfo.go.kr/a"]


def test_redirect_loop_capped_at_max_hops(sources_crawl):
    url = "https://www.bizinfo.go.kr/loop"
    do_request, requested = make_transport({url: (302, "", url)})
    with pytest.raises(RuntimeError, match="redirect chain exceeded"):
        sources_crawl.follow_redirects(do_request, url)
    assert len(requested) == sources_crawl.MAX_REDIRECTS + 1


def test_redirect_cross_source_blocked_with_per_source_domains(sources_crawl):
    """A bizinfo crawl may not be redirected even to another PERMITTED source."""
    do_request, requested = make_transport({
        "https://www.bizinfo.go.kr/a": (302, "", "https://www.nipa.kr/x"),
    })
    with pytest.raises(RuntimeError, match="blocked"):
        sources_crawl.follow_redirects(
            do_request, "https://www.bizinfo.go.kr/a",
            allowed_domains=sources_crawl.SOURCE_DOMAINS["bizinfo"])
    assert requested == ["https://www.bizinfo.go.kr/a"]


def test_source_domains_cover_all_sources(sources_crawl):
    assert set(sources_crawl.SOURCE_DOMAINS) == set(sources_crawl.SOURCES)


def test_non_redirect_response_passes_through(sources_crawl):
    do_request, requested = make_transport({
        "https://www.bizinfo.go.kr/a": (404, "not found", None),
    })
    assert sources_crawl.follow_redirects(
        do_request, "https://www.bizinfo.go.kr/a") == (404, "not found")


# ------------------------------------------------------- kstartup_crawl.py

def ks_transport(script):
    requested = []

    def do_request(url):
        requested.append(url)
        return script[url]

    return do_request, requested


def test_kstartup_redirect_to_external_blocked(kstartup_crawl):
    url = "https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do"
    do_request, requested = ks_transport({
        url: (302, "", "https://evil.example/phish"),
    })
    with pytest.raises(RuntimeError, match="redirect to non-source url blocked"):
        kstartup_crawl.follow_redirects(do_request, url)
    assert requested == [url]  # external host never contacted


def test_kstartup_redirect_allowed_variant_succeeds(kstartup_crawl):
    do_request, requested = ks_transport({
        "https://k-startup.go.kr/a": (301, "", "https://www.k-startup.go.kr/a"),
        "https://www.k-startup.go.kr/a": (200, "ok", None),
    })
    status, text = kstartup_crawl.follow_redirects(do_request, "https://k-startup.go.kr/a")
    assert (status, text) == (200, "ok")


def test_kstartup_host_allowed_scope(kstartup_crawl):
    assert kstartup_crawl.host_allowed("https://www.k-startup.go.kr/x")
    assert not kstartup_crawl.host_allowed("https://www.bizinfo.go.kr/x")  # other sources: no
    assert not kstartup_crawl.host_allowed("http://www.k-startup.go.kr/x")
    assert not kstartup_crawl.host_allowed("https://k-startup.go.kr@evil.example/x")


def test_kstartup_cmd_list_redirect_to_external_fails_closed(
        kstartup_crawl, monkeypatch, tmp_path):
    """End-to-end: a mid-crawl redirect violation is a failed request →
    partial run (exit 2), and the external response is never saved."""
    import json
    import types

    def fetch(url):
        raise RuntimeError("redirect to non-source url blocked: https://evil.example/")

    monkeypatch.setattr(kstartup_crawl, "make_fetcher", lambda: (fetch, "fake"))
    args = types.SimpleNamespace(
        output=str(tmp_path / "kstartup_all.jsonl"), max_pages=3, min_expected=0)
    with pytest.raises(SystemExit) as e:
        kstartup_crawl.cmd_list(args)
    assert e.value.code == 2
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    (run,) = manifest["runs"]
    assert run["status"] == "partial"
    assert run["stop_reason"] == "network-error"
    assert run["pages_fetched"] == 0


def test_sources_detail_redirect_failure_exits_2_and_saves_nothing(
        sources_crawl, monkeypatch, tmp_path):
    """detail mode: a fetch that raises on a blocked redirect → FAIL + exit 2,
    no external content written."""
    def fetch(url, data=None):
        raise RuntimeError("redirect to non-source url blocked: https://evil.example/")

    outdir = tmp_path / "details"
    with pytest.raises(SystemExit) as e:
        sources_crawl.cmd_detail(
            fetch, ["https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId=PBLN_1"],
            str(outdir))
    assert e.value.code == 2
    assert list(outdir.iterdir()) == []  # nothing saved
