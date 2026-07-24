"""라운드 6 하드닝 회귀 테스트 (ir-search) — Codex 교차검증(2026-07-24) 실결함 고정.

  #1 200 위장 소프트 차단(CAPTCHA/HTML) — detail·첨부 모두 정상 처리 금지
  #2 process_attachments 비-Manual 예외 → v2/incomplete 병합(과거 v3 잔존 금지)
  #3 SOURCE_DOMAINS exact-host — pms.kocca.kr 등 서브도메인 유출 차단
  #4 1차 페이지 401/403 → 수동 전환(exit 3), partial(exit 2) 강등 금지
  #6 recover_filename가 정상 latin-1 이름(AI·DX.pdf)을 훼손하지 않음
  #7 robots 검사가 잘못된 percent 인코딩에 fail-closed
"""
import email.message
import hashlib
import json

import pytest

BIZ_URL = ("https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do"
           "?pblancId=PBLN_000000000001")
BIZ_HTML_ONE_ATTACH = """
<html><head><title>x</title></head><body>
<div id="header">메뉴 조회수 12345</div>
<div class="view_cont">
  <h3>[합성] 창업 바우처 공고</h3>
  <a href="/cmm/fms/getFile.do?atchFileId=F1&fileSn=0">공고문.pdf</a>
</div>
<div id="footer">푸터</div>
</body></html>
"""


class FakeResp:
    def __init__(self, data, url, headers=None):
        self._data, self._pos, self._url = data, 0, url
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


def _biz_jsonl(tmp_path):
    p = tmp_path / "list.jsonl"
    p.write_text(json.dumps({
        "source": "bizinfo", "id": "PBLN_000000000001", "title": "t",
        "content_hash": "OLD", "hash_version": 3, "attachments": [],
        "attachments_complete": True}, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


# ---- #3 SOURCE_DOMAINS exact-host ----
def test_source_domains_exact_host_blocks_subdomain(sources_crawl):
    dom = sources_crawl.SOURCE_DOMAINS["kocca"]
    assert sources_crawl.host_allowed("https://www.kocca.kr/list.do", dom)
    assert sources_crawl.host_allowed("https://kocca.kr/list.do", dom)
    # 다른 서브도메인(별개 시스템)은 차단
    assert not sources_crawl.host_allowed("https://pms.kocca.kr/x", dom)
    assert not sources_crawl.host_allowed("https://evil.kocca.kr/x", dom)


# ---- #6 recover_filename 정상 이름 보존 ----
def test_recover_filename_keeps_valid_latin1_name(attach_download):
    # 0xB7(·) 하나뿐인 정상 이름 — cp949 폴백이 한자 쓰레기로 훼손하면 안 됨
    assert attach_download.recover_filename("AI·DX.pdf") == "AI·DX.pdf"
    assert attach_download.recover_filename("기술·개발.pdf") == "기술·개발.pdf"


def test_recover_filename_still_fixes_cp949_mojibake(attach_download):
    # SMTECH류: cp949 바이트가 latin-1로 잘못 온 경우 → 한글 복원되면 채택
    mojibake = "사업".encode("cp949").decode("latin-1")
    assert attach_download.recover_filename(mojibake) == "사업"


# ---- #7 robots 잘못된 인코딩 fail-closed ----
def test_robots_malformed_encoding_fail_closed(attach_download):
    dis = ("/uploads",)
    # 정상 경로는 허용
    assert attach_download.robots_allowed("https://x.go.kr/cmm/fms/a.pdf", dis)
    # 잘못된 percent 인코딩(%FF 유효hex-무효utf8, %ZZ 무효hex, 절단)은 전부 거부
    assert not attach_download.robots_allowed("https://x.go.kr/%FFuploads", dis)
    assert not attach_download.robots_allowed("https://x.go.kr/%ZZuploads", dis)
    assert not attach_download.robots_allowed("https://x.go.kr/%E0%A4uploads", dis)
    assert not attach_download.robots_allowed("https://x.go.kr/foo%", dis)
    # 중첩 인코딩(%25ZZ → 1회 디코드 후 %ZZ)도 각 단계 검사로 거부
    assert not attach_download.robots_allowed("https://x.go.kr/%25ZZuploads", dis)


# ---- #1a 소프트 차단 감지 헬퍼 ----
def test_looks_like_html_error_helper(attach_download):
    f = attach_download._looks_like_html_error
    assert f(b"<!DOCTYPE html><html>Access Denied", "text/html", "00_공고문.pdf")
    assert f(b"<html><head>CAPTCHA", "", "01_x.hwp")
    # <body>·주석·BOM 선행·URL기반 파일명(확장자 없음)도 잡는다
    assert f(b"<body>CAPTCHA</body>", "", "00_getFile.do")
    assert f(b"<!-- blocked -->\n<html>", "", "00_x.pdf")
    assert f(b"\xef\xbb\xbf<html>denied", "", "00_x.hwp")
    # 정상 PDF/OLE/ZIP 바이트는 통과
    assert not f(b"%PDF-1.7\n...", "application/pdf", "00_공고문.pdf")
    assert not f(b"PK\x03\x04...", "", "00_x.hwpx")
    # 마크업 확장자 첨부(.html/.xml/.svg)는 정상
    assert not f(b"<html>", "text/html", "00_page.html")
    assert not f(b"<?xml version='1.0'?>", "", "00_data.xml")


def test_looks_blocked_detail_captcha(attach_download):
    assert attach_download.looks_blocked("<html><body>CAPTCHA 입력</body></html>")
    assert attach_download.looks_blocked("<h1>Access Denied</h1>")
    assert not attach_download.looks_blocked("<html>정상 공고 본문</html>")


def test_follow_redirects_403_raises_manual(attach_download):
    # 양 백엔드 공통: do_request가 403 상태를 반환하면 ManualEscalation(→exit 3)
    import sys, importlib
    sc = importlib.import_module("sources_crawl")
    def fake_do(url, data=None):
        return 403, "", None
    with pytest.raises(attach_download.ManualEscalation):
        sc.follow_redirects(fake_do, "https://www.bizinfo.go.kr/x", None,
                            ("bizinfo.go.kr",))


# ---- #1b 첨부 소프트 차단(200 HTML) → 저장 실패 ----
def test_attachment_soft_block_html_not_saved(sources_crawl, attach_download,
                                              monkeypatch, tmp_path):
    html_error = b"<!DOCTYPE html><html><body>Access Denied / CAPTCHA</body></html>"
    monkeypatch.setattr(attach_download, "_urlopen",
                        lambda req, timeout: FakeResp(html_error, req.full_url))
    jsonl = _biz_jsonl(tmp_path)
    with pytest.raises(SystemExit) as e:
        sources_crawl.cmd_detail(
            lambda url, data=None: (200, BIZ_HTML_ONE_ATTACH), [BIZ_URL],
            str(tmp_path / "details"),
            download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    assert e.value.code == 2  # partial (첨부 미검증)
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    assert rec["attachments_complete"] is False
    (att,) = rec["attachments"]
    assert att["download_status"] != "ok"  # HTML 오류를 첨부로 저장하지 않음
    # 저장 폴더에 .pdf가 남지 않았는지
    saved = list((tmp_path / "atts").rglob("*.pdf"))
    assert saved == []


# ---- #1c detail 200 위장 차단 → 수동 전환(exit 3) ----
def test_detail_soft_block_captcha_manual(sources_crawl, tmp_path):
    captcha = "<html><body>자동입력 방지(CAPTCHA) 문자를 입력하세요</body></html>"
    with pytest.raises(SystemExit) as e:
        sources_crawl.cmd_detail(
            lambda url, data=None: (200, captcha), [BIZ_URL],
            str(tmp_path / "details"), download_dir=str(tmp_path / "atts"),
            merge_into=str(_biz_jsonl(tmp_path)))
    assert e.value.code == 3  # 우회 금지·수동 전환


# ---- #4 1차 페이지 403 → 수동 전환(exit 3) ----
def test_detail_primary_403_manual(sources_crawl, tmp_path):
    with pytest.raises(SystemExit) as e:
        sources_crawl.cmd_detail(
            lambda url, data=None: (403, ""), [BIZ_URL],
            str(tmp_path / "details"), download_dir=str(tmp_path / "atts"),
            merge_into=str(_biz_jsonl(tmp_path)))
    assert e.value.code == 3


# ---- kstartup 하드닝: detail 403/차단 → exit 3, generic 예외 → merge ----
def test_kstartup_detail_403_manual(kstartup_crawl, attach_download, monkeypatch, tmp_path):
    import types

    def boom(url):
        raise attach_download.ManualEscalation("HTTP 403 — 차단 신호")

    monkeypatch.setattr(kstartup_crawl, "make_fetcher", lambda: (boom, "test"))
    args = types.SimpleNamespace(pbancSn=["7"], output=str(tmp_path / "d"),
                                 download_dir=None, merge_into=None)
    with pytest.raises(SystemExit) as e:
        kstartup_crawl.cmd_detail(args)
    assert e.value.code == 3


def test_kstartup_list_403_manual_no_crash(kstartup_crawl, attach_download,
                                           monkeypatch, tmp_path):
    import types

    def boom(url):
        raise attach_download.ManualEscalation("HTTP 403 — 차단 신호")

    monkeypatch.setattr(kstartup_crawl, "make_fetcher", lambda: (boom, "test"))
    args = types.SimpleNamespace(output=str(tmp_path / "list.jsonl"), max_pages=1)
    with pytest.raises(SystemExit) as e:
        kstartup_crawl.cmd_list(args)
    assert e.value.code == 3  # 'blocked' status ValueError 크래시(exit 1) 아님


def test_sources_list_200_softblock_manual(sources_crawl, monkeypatch, tmp_path):
    # 멀티소스 목록 래퍼: 200 위장 CAPTCHA → parse-failure(partial) 아니라 수동 전환(exit 3)
    captcha = "<html><body>자동입력 방지(CAPTCHA)</body></html>"
    monkeypatch.setattr(sources_crawl, "make_fetcher",
                        lambda *a, **k: ((lambda url, data=None: (200, captcha)), "fake"))
    out = tmp_path / "biz.jsonl"
    monkeypatch.setattr("sys.argv", ["sources_crawl.py", "list", "bizinfo", "-o", str(out)])
    with pytest.raises(SystemExit) as e:
        sources_crawl.main()
    assert e.value.code == 3  # 차단 신호 — partial(2) 강등 금지
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    (run,) = manifest["runs"]
    assert run["status"] == "manual" and run["exit_code"] == 3


def test_kstartup_list_200_softblock_manual(kstartup_crawl, monkeypatch, tmp_path):
    captcha = "<html><body>CAPTCHA 자동입력 방지</body></html>"
    monkeypatch.setattr(kstartup_crawl, "make_fetcher",
                        lambda: ((lambda url: (200, captcha)), "test"))
    import types
    args = types.SimpleNamespace(output=str(tmp_path / "l.jsonl"), max_pages=1)
    with pytest.raises(SystemExit) as e:
        kstartup_crawl.cmd_list(args)
    assert e.value.code == 3  # partial(2) 아님 — 수동 전환


def test_kstartup_detail_soft_block_manual(kstartup_crawl, monkeypatch, tmp_path):
    import types
    captcha = "<html><body>자동입력 방지(CAPTCHA)</body></html>"
    monkeypatch.setattr(kstartup_crawl, "make_fetcher",
                        lambda: ((lambda url: (200, captcha)), "test"))
    args = types.SimpleNamespace(pbancSn=["7"], output=str(tmp_path / "d"),
                                 download_dir=str(tmp_path / "a"),
                                 merge_into=None)
    with pytest.raises(SystemExit) as e:
        kstartup_crawl.cmd_detail(args)
    assert e.value.code == 3


# ---- #2 process_attachments 비-Manual 예외 → v2/incomplete 병합 ----
def test_process_attachments_generic_error_merges_v2(sources_crawl, attach_download,
                                                     monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("subdir_symlink_blocked: evil")

    monkeypatch.setattr(attach_download, "process_attachments", boom)
    jsonl = _biz_jsonl(tmp_path)
    with pytest.raises(SystemExit) as e:
        sources_crawl.cmd_detail(
            lambda url, data=None: (200, BIZ_HTML_ONE_ATTACH), [BIZ_URL],
            str(tmp_path / "details"),
            download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    assert e.value.code == 2  # partial, 크래시 아님
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    # 과거 v3/complete:true가 잔존하지 않고 v2/incomplete로 갱신됨
    assert rec["attachments_complete"] is False
    assert rec.get("hash_version") == 2
    assert rec["content_hash"] != "OLD"
