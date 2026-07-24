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


# --------------------------------------------------------- recover_filename
# 버그2 회귀: SMTECH는 일부 첨부 파일명을 CP949(EUC-KR) + `&amp;` HTML
# 엔티티로 보낸다. latin-1→utf-8만 하던 recover_filename이 CP949를 복구하지
# 못해 mojibake로 저장되던 것을 고정한다. 정상 UTF-8/한글 이름 무회귀도 검증.


def test_recover_filename_cp949_mojibake_restored(attach_download):
    """(a) CP949 바이트를 latin-1로 디코드한 mojibake → 올바른 한글 복구."""
    original = "2026년 중소기업 지원.hwp"
    mojibake = original.encode("cp949").decode("latin-1")
    assert mojibake != original  # 실제로 깨진 상태에서 출발
    assert attach_download.recover_filename(mojibake) == original


def test_recover_filename_valid_utf8_hangul_untouched(attach_download):
    """(b) 이미 정상인 UTF-8 한글 이름 → CP949로 오해석해 훼손하지 않는다."""
    for name in ("서식1.hwp", "붙임_2._신청서.hwp", "공고문.pdf"):
        assert attach_download.recover_filename(name) == name


def test_recover_filename_html_entity_amp_decoded(attach_download):
    """(c) `&amp;` HTML 엔티티 → `&`로 디코드."""
    assert attach_download.recover_filename("R&amp;D_지원.hwp") == "R&D_지원.hwp"
    # CP949 mojibake + &amp;가 함께 온 SMTECH 실측 형태도 복구
    smtech = "2026년 R&D.hwp"
    moji = smtech.encode("cp949").decode("latin-1").replace("&", "&amp;")
    assert attach_download.recover_filename(moji) == smtech


def test_recover_filename_utf8_as_latin1_still_recovered(attach_download):
    """(d) UTF-8-as-latin1(기존 bizinfo 케이스) → 무회귀로 여전히 복구."""
    original = "공고문.pdf"
    mojibake = original.encode("utf-8").decode("latin-1")
    assert attach_download.recover_filename(mojibake) == original


def test_recover_filename_ascii_and_percent_unquote(attach_download):
    """순수 ASCII는 그대로, %-인코딩은 unquote(현행 유지)."""
    assert attach_download.recover_filename("form.pdf") == "form.pdf"
    assert attach_download.recover_filename("%EA%B3%B5%EA%B3%A0.hwp") == "공고.hwp"
    assert attach_download.recover_filename(None) is None


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


# ---- robots는 모든 리다이렉트 홉에 적용 (Codex 게이트 #1 회귀) ----------------

def test_redirect_into_robots_disallowed_path_blocked(attach_download, monkeypatch):
    """/cmm/fms/ → 302 → /uploads/… 동일 호스트 리다이렉트로 robots를 우회할 수
    없다 — /uploads에는 요청 자체가 나가지 않는다."""
    requested = []

    def fake_urlopen(req, timeout):
        requested.append(req.full_url)
        raise http_error(req.full_url, 302,
                         "https://www.bizinfo.go.kr/uploads/secret.hwp")

    monkeypatch.setattr(attach_download, "_urlopen", fake_urlopen)
    with pytest.raises(attach_download.RedirectBlocked, match="robots 불허"):
        attach_download.open_validated(
            "https://www.bizinfo.go.kr/cmm/fms/getFile.do?id=1", HOSTS, 30,
            robots_disallowed=BIZ_ROBOTS)
    assert requested == ["https://www.bizinfo.go.kr/cmm/fms/getFile.do?id=1"]
    assert not any("/uploads" in u for u in requested)


def test_redirect_robots_hop_yields_blocked_redirect_status(
        attach_download, monkeypatch, tmp_path):
    """process_attachments 경유: robots 불허 경로로의 리다이렉트는
    blocked_redirect로 기록되고 해시 목록에 들어가지 않는다."""
    def fake_urlopen(req, timeout):
        if "/uploads" in req.full_url:
            raise AssertionError("robots 불허 경로가 요청됐다")
        raise http_error(req.full_url, 302, "/uploads/evil.hwp")

    monkeypatch.setattr(attach_download, "_urlopen", fake_urlopen)
    atts = [{"url": "https://www.bizinfo.go.kr/cmm/fms/f", "filename": "f.pdf"}]
    hashes = attach_download.process_attachments(atts, tmp_path, 0, HOSTS, BIZ_ROBOTS)
    assert hashes == []
    assert atts[0]["download_status"] == "blocked_redirect"


def test_initial_robots_disallowed_blocked_in_open_validated(attach_download,
                                                             monkeypatch):
    """심층 방어: process_attachments의 사전 skip을 우회해 직접 불러도 차단."""
    def boom(req, timeout):
        raise AssertionError("robots 불허 경로에 요청이 나갔다")

    monkeypatch.setattr(attach_download, "_urlopen", boom)
    with pytest.raises(attach_download.RedirectBlocked, match="robots 불허"):
        attach_download.open_validated(
            "https://www.bizinfo.go.kr/uploads/a.hwp", HOSTS, 30,
            robots_disallowed=BIZ_ROBOTS)


# ---- 공고별 하위 폴더 — 동명 첨부 충돌 방지 (Codex 게이트 #5 회귀) -------------

def test_same_filename_across_announcements_no_overwrite(attach_download,
                                                         monkeypatch, tmp_path):
    """두 공고의 동명 첨부(공고문.pdf)가 서로 덮어쓰지 않고 공고별 폴더에
    분리 저장되며, 기록된 local_path/sha256이 실제 파일과 일치한다."""
    def fake_urlopen(req, timeout):
        data = b"DATA-A" if "aaa" in req.full_url else b"DATA-B"
        resp = FakeResp(data, headers={
            "Content-Disposition": 'attachment; filename="공고문.pdf"'},
            url=req.full_url)
        return resp

    monkeypatch.setattr(attach_download, "_urlopen", fake_urlopen)
    a = [{"url": "https://www.bizinfo.go.kr/cmm/fms/aaa", "filename": "공고문.pdf"}]
    b = [{"url": "https://www.bizinfo.go.kr/cmm/fms/bbb", "filename": "공고문.pdf"}]
    attach_download.process_attachments(a, tmp_path, 0, HOSTS, BIZ_ROBOTS,
                                        subdir="PBLN_A")
    attach_download.process_attachments(b, tmp_path, 0, HOSTS, BIZ_ROBOTS,
                                        subdir="PBLN_B")
    pa, pb = a[0]["local_path"], b[0]["local_path"]
    assert pa != pb
    import pathlib
    assert pathlib.Path(pa).read_bytes() == b"DATA-A"  # A가 B에 덮이지 않았다
    assert pathlib.Path(pb).read_bytes() == b"DATA-B"
    assert a[0]["sha256"] == hashlib.sha256(b"DATA-A").hexdigest()
    assert b[0]["sha256"] == hashlib.sha256(b"DATA-B").hexdigest()
    assert pathlib.Path(pa).parent.name == "PBLN_A"
    assert pathlib.Path(pb).parent.name == "PBLN_B"


def test_safe_subdir_sanitizes_separators(attach_download):
    assert "/" not in attach_download.safe_subdir("../../etc")
    assert attach_download.safe_subdir("PBLN_000000000001") == "PBLN_000000000001"


# ---- percent-인코딩 robots 우회 차단 (Codex 재심사 #1 회귀) --------------------

def test_robots_percent_encoded_path_blocked(attach_download):
    """/%75ploads == /uploads — 인코딩 위장이 robots 검사를 통과하면 안 된다."""
    assert not attach_download.robots_allowed(
        "https://www.bizinfo.go.kr/%75ploads/a.hwp", BIZ_ROBOTS)
    assert not attach_download.robots_allowed(
        "https://www.bizinfo.go.kr/%2575ploads/a.hwp", BIZ_ROBOTS)  # 이중 인코딩
    assert not attach_download.robots_allowed(
        "https://www.bizinfo.go.kr/x/../uploads/a.hwp", BIZ_ROBOTS)  # normpath
    assert not attach_download.robots_allowed(
        "https://www.bizinfo.go.kr/%2Fuploads/a.hwp", BIZ_ROBOTS)  # → //uploads
    assert not attach_download.robots_allowed(
        "https://www.bizinfo.go.kr//uploads/a.hwp", BIZ_ROBOTS)  # 직접 이중 슬래시
    assert not attach_download.robots_allowed(
        "https://www.bizinfo.go.kr/%2F%2Fuploads/a.hwp", BIZ_ROBOTS)  # ///uploads
    # 정상 경로는 여전히 허용
    assert attach_download.robots_allowed(
        "https://www.bizinfo.go.kr/cmm/fms/getFile.do", BIZ_ROBOTS)


def test_redirect_to_percent_encoded_robots_path_blocked(attach_download,
                                                         monkeypatch):
    """/cmm/fms/ → 302 → /%75ploads/… — 디코딩하면 robots 불허. 요청 금지."""
    requested = []

    def fake_urlopen(req, timeout):
        requested.append(req.full_url)
        if "ploads" in req.full_url:
            raise AssertionError("인코딩 위장 robots 불허 경로가 요청됐다")
        raise http_error(req.full_url, 302,
                         "https://www.bizinfo.go.kr/%75ploads/encoded.hwp")

    monkeypatch.setattr(attach_download, "_urlopen", fake_urlopen)
    with pytest.raises(attach_download.RedirectBlocked, match="robots 불허"):
        attach_download.open_validated(
            "https://www.bizinfo.go.kr/cmm/fms/start", HOSTS, 30,
            robots_disallowed=BIZ_ROBOTS)
    assert requested == ["https://www.bizinfo.go.kr/cmm/fms/start"]


def test_robots_excessive_multi_encoding_fail_closed(attach_download):
    """디코딩 상한(5회) 내 고정점 미도달 — fail-closed로 거부."""
    deep = "uploads"
    for _ in range(7):
        import urllib.parse as up
        deep = up.quote(deep, safe="")
    assert not attach_download.robots_allowed(
        f"https://www.bizinfo.go.kr/{deep}/a.hwp", BIZ_ROBOTS)


# ---- symlink 하위 폴더 탈출 차단 (Codex 재심사 #2 회귀) ------------------------

def test_symlinked_subdir_blocked(attach_download, monkeypatch, tmp_path):
    """사전 배치된 <download-dir>/<공고ID> symlink로 base 밖에 기록 금지."""
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "atts"
    base.mkdir()
    (base / "PBLN_X").symlink_to(outside)

    def boom(req, timeout):
        raise AssertionError("symlink 탈출 상태에서 다운로드가 시도됐다")

    monkeypatch.setattr(attach_download, "_urlopen", boom)
    atts = [{"url": "https://www.bizinfo.go.kr/cmm/fms/f", "filename": "f.pdf"}]
    with pytest.raises(RuntimeError, match="subdir_symlink_blocked"):
        attach_download.process_attachments(atts, base, 0, HOSTS, BIZ_ROBOTS,
                                            subdir="PBLN_X")
    assert list(outside.iterdir()) == []  # base 밖에 아무것도 안 쓰였다


def test_symlinked_base_parent_escape_blocked(attach_download, monkeypatch,
                                              tmp_path):
    """realpath 재검증 — 정제된 subdir 이름이 그 자체로는 안전해도, 최종
    realpath가 base 밖이면 거부한다 (환경 조작 방어)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "atts"
    base.mkdir()
    (base / "PBLN_Y").symlink_to(outside)
    # is_symlink 검사를 우회하는 가상 시나리오를 realpath 검증이 잡는지 확인
    monkeypatch.setattr(attach_download.pathlib.Path, "is_symlink",
                        lambda self: False)
    atts = [{"url": "https://www.bizinfo.go.kr/cmm/fms/f", "filename": "f.pdf"}]
    monkeypatch.setattr(attach_download, "_urlopen",
                        lambda req, timeout: (_ for _ in ()).throw(
                            AssertionError("다운로드 시도 금지")))
    with pytest.raises(RuntimeError, match="subdir_escape_blocked"):
        attach_download.process_attachments(atts, base, 0, HOSTS, BIZ_ROBOTS,
                                            subdir="PBLN_Y")
    assert list(outside.iterdir()) == []


def test_normal_subdir_still_works_after_hardening(attach_download, monkeypatch,
                                                   tmp_path):
    monkeypatch.setattr(attach_download, "_urlopen",
                        lambda req, timeout: FakeResp(b"OK", url=req.full_url))
    atts = [{"url": "https://www.bizinfo.go.kr/cmm/fms/f", "filename": "f.pdf"}]
    hashes = attach_download.process_attachments(atts, tmp_path, 0, HOSTS,
                                                 BIZ_ROBOTS, subdir="PBLN_OK")
    assert len(hashes) == 1
    assert atts[0]["download_status"] == "ok"
    import pathlib
    assert pathlib.Path(atts[0]["local_path"]).parent.name == "PBLN_OK"
