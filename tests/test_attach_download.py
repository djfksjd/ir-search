"""attach_download.py 계약 테스트 (no network).

sole-search에서 이식한 첨부 슬라이스의 보안·해시 계약을 검증한다:
사전검증 리다이렉트(요청 전 차단), 50MB 스트리밍 상한, sha256,
latin-1→UTF-8 파일명 복구, robots 불허 경로 skip, hash v3 산식.
실제 소켓은 attach_download._urlopen 단일 통로를 monkeypatch해 봉쇄한다.
"""
import email.message
import hashlib
import io
import urllib.error

import pytest

HOSTS = ("bizinfo.go.kr",)


class FakeResp:
    """urlopen 응답 대역 — read 스트리밍, 헤더, geturl, 컨텍스트 매니저."""

    def __init__(self, data=b"PDFDATA", headers=None, url="https://www.bizinfo.go.kr/x"):
        self._data = data
        self._pos = 0
        self._url = url
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(url, code, location=None):
    hdrs = email.message.Message()
    if location:
        hdrs["Location"] = location
    return urllib.error.HTTPError(url, code, "err", hdrs, io.BytesIO(b""))


# ------------------------------------------------- open_validated / redirects

def test_redirect_to_external_blocked_before_request(attach_download, monkeypatch):
    requested = []

    def fake_urlopen(req, timeout):
        requested.append(req.full_url)
        raise http_error(req.full_url, 302, "https://evil.example/steal")

    monkeypatch.setattr(attach_download, "_urlopen", fake_urlopen)
    with pytest.raises(attach_download.RedirectBlocked, match="리다이렉트 대상 불허"):
        attach_download.open_validated("https://www.bizinfo.go.kr/a", HOSTS, 30)
    # 외부 호스트로는 요청 자체가 나가지 않았다
    assert requested == ["https://www.bizinfo.go.kr/a"]


def test_redirect_http_downgrade_blocked(attach_download, monkeypatch):
    monkeypatch.setattr(
        attach_download, "_urlopen",
        lambda req, timeout: (_ for _ in ()).throw(
            http_error(req.full_url, 301, "http://www.bizinfo.go.kr/a")))
    with pytest.raises(attach_download.RedirectBlocked):
        attach_download.open_validated("https://www.bizinfo.go.kr/a", HOSTS, 30)


def test_redirect_allowed_hop_followed_then_ok(attach_download, monkeypatch):
    resp = FakeResp()

    def fake_urlopen(req, timeout):
        if req.full_url.endswith("/a"):
            raise http_error(req.full_url, 302, "/b")  # 상대 Location 해석
        return resp

    monkeypatch.setattr(attach_download, "_urlopen", fake_urlopen)
    assert attach_download.open_validated(
        "https://www.bizinfo.go.kr/a", HOSTS, 30) is resp


def test_redirect_loop_capped(attach_download, monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(req.full_url)
        raise http_error(req.full_url, 302, req.full_url)

    monkeypatch.setattr(attach_download, "_urlopen", fake_urlopen)
    with pytest.raises(attach_download.RedirectBlocked, match="홉 초과"):
        attach_download.open_validated("https://www.bizinfo.go.kr/loop", HOSTS, 30)
    assert len(calls) == attach_download.MAX_REDIRECTS + 1


def test_initial_url_must_pass_allowlist(attach_download, monkeypatch):
    def boom(req, timeout):
        raise AssertionError("request must not be sent")

    monkeypatch.setattr(attach_download, "_urlopen", boom)
    with pytest.raises(attach_download.RedirectBlocked):
        attach_download.open_validated("http://www.bizinfo.go.kr/a", HOSTS, 30)
    with pytest.raises(attach_download.RedirectBlocked):
        attach_download.open_validated("https://evil.example/a", HOSTS, 30)


def test_host_allowed_spoof_resistant(attach_download):
    ok = attach_download.host_allowed
    assert ok("https://www.bizinfo.go.kr/x", HOSTS)
    assert not ok("https://evilbizinfo.go.kr/x", HOSTS)
    assert not ok("https://bizinfo.go.kr@evil.example/x", HOSTS)
    assert not ok("http://www.bizinfo.go.kr/x", HOSTS)


# ------------------------------------------------------- download_attachment

def test_download_ok_sha256_and_filename(attach_download, monkeypatch, tmp_path):
    data = b"%PDF-1.4 synthetic"
    mojibake = "공고문.pdf".encode("utf-8").decode("latin-1")
    resp = FakeResp(data, headers={
        "Content-Disposition": f'attachment; filename="{mojibake}"'})
    monkeypatch.setattr(attach_download, "_urlopen", lambda req, timeout: resp)
    path = attach_download.download_attachment(
        "https://www.bizinfo.go.kr/cmm/fms/getFile.do?id=1", tmp_path, "fb", 0, HOSTS)
    assert path.read_bytes() == data
    # latin-1→UTF-8 모지바케 복구 + 순번 프리픽스
    assert path.name == "00_공고문.pdf"
    assert hashlib.sha256(data).hexdigest() == hashlib.sha256(path.read_bytes()).hexdigest()


def test_download_content_length_over_cap_rejected(attach_download, monkeypatch, tmp_path):
    resp = FakeResp(b"x", headers={
        "Content-Length": str(attach_download.MAX_ATTACH_BYTES + 1)})
    monkeypatch.setattr(attach_download, "_urlopen", lambda req, timeout: resp)
    with pytest.raises(RuntimeError, match="상한 초과"):
        attach_download.download_attachment(
            "https://www.bizinfo.go.kr/cmm/fms/f", tmp_path, "fb", 0, HOSTS)
    assert list(tmp_path.iterdir()) == []


def test_download_streaming_cap_deletes_partial_file(attach_download, monkeypatch, tmp_path):
    monkeypatch.setattr(attach_download, "MAX_ATTACH_BYTES", 10)
    resp = FakeResp(b"x" * 32)  # Content-Length 없이 상한 초과 스트림
    monkeypatch.setattr(attach_download, "_urlopen", lambda req, timeout: resp)
    with pytest.raises(RuntimeError, match="상한 초과"):
        attach_download.download_attachment(
            "https://www.bizinfo.go.kr/cmm/fms/f", tmp_path, "fb", 0, HOSTS)
    assert list(tmp_path.iterdir()) == []  # 부분 파일 잔존 금지


def test_download_403_escalates_manual(attach_download, monkeypatch, tmp_path):
    monkeypatch.setattr(
        attach_download, "_urlopen",
        lambda req, timeout: (_ for _ in ()).throw(http_error(req.full_url, 403)))
    with pytest.raises(attach_download.ManualEscalation):
        attach_download.download_attachment(
            "https://www.bizinfo.go.kr/cmm/fms/f", tmp_path, "fb", 0, HOSTS)


def test_safe_filename_blocks_traversal(attach_download):
    assert "/" not in attach_download.safe_filename("../../etc/passwd", 3)
    assert attach_download.safe_filename("../../etc/passwd", 3).startswith("03_")
    assert attach_download.safe_filename(None, 1) == "01_attach"
    assert attach_download.safe_filename("a\\..\\b.hwp", 2).endswith("b.hwp")


# ------------------------------------------------------- process_attachments

BIZ_ROBOTS = ("/upload", "/download")


def test_robots_disallowed_skipped_without_request(attach_download, monkeypatch, tmp_path):
    def boom(req, timeout):
        raise AssertionError("robots 불허 경로에 요청이 나갔다")

    monkeypatch.setattr(attach_download, "_urlopen", boom)
    atts = [{"url": "https://www.bizinfo.go.kr/uploads/a.hwp", "filename": "a.hwp"}]
    hashes = attach_download.process_attachments(
        atts, tmp_path, 0, HOSTS, BIZ_ROBOTS)
    assert hashes == []
    assert atts[0]["download_status"] == "skipped_robots"
    assert "sha256" not in atts[0]


def test_process_mixed_ok_blocked_failed(attach_download, monkeypatch, tmp_path):
    data = b"FILE"

    def fake_urlopen(req, timeout):
        url = req.full_url
        if "good" in url:
            return FakeResp(data, url=url)
        if "redir" in url:
            raise http_error(url, 302, "https://evil.example/x")
        raise http_error(url, 500)

    monkeypatch.setattr(attach_download, "_urlopen", fake_urlopen)
    atts = [
        {"url": "https://www.bizinfo.go.kr/cmm/fms/good", "filename": "g.pdf"},
        {"url": "https://www.bizinfo.go.kr/cmm/fms/redir", "filename": "r.pdf"},
        {"url": "https://www.bizinfo.go.kr/cmm/fms/boom", "filename": "b.pdf"},
    ]
    hashes = attach_download.process_attachments(atts, tmp_path, 0, HOSTS, BIZ_ROBOTS)
    assert atts[0]["download_status"] == "ok"
    assert atts[0]["sha256"] == hashlib.sha256(data).hexdigest()
    assert atts[1]["download_status"] == "blocked_redirect"
    assert atts[2]["download_status"] == "failed"
    assert hashes == [atts[0]["sha256"]]


def test_content_hash_of_is_order_independent(attach_download):
    a = attach_download.content_hash_of("body", ["h2", "h1"])
    b = attach_download.content_hash_of("body", ["h1", "h2"])
    assert a == b
    assert a != attach_download.content_hash_of("body2", ["h1", "h2"])
    # sole-search sbiz_crawl.content_hash_of와 동일 산식(고정 벡터)
    expected = hashlib.sha256("body\nh1\nh2".encode()).hexdigest()
    assert a == expected
