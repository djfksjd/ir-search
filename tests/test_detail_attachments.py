"""detail --download-dir 통합 테스트 (no network) — bizinfo·K-Startup.

hash v3 전환(전부 성공 시에만), robots 생략 시 v2 유지 + exit 2,
--merge-into 병합, K-Startup /afile robots 불허(링크만) 계약을 검증한다.
"""
import email.message
import hashlib
import json
import types

import pytest

BIZ_DETAIL_HTML = """
<html><head><title>x</title></head><body>
<div id="header">메뉴 조회수 12345</div>
<div class="view_cont">
  <h3>[합성] 창업 바우처 공고</h3>
  <p>신청대상: 예비창업자 포함</p>
  <a href="/cmm/fms/getFile.do?atchFileId=F1&fileSn=0">공고문.pdf</a>
  <a href="/uploads/legacy/양식.hwp">양식.hwp</a>
</div>
<div id="footer">푸터</div>
</body></html>
"""

KS_DETAIL_HTML = """
<html><body>
<div class="content_wrap">
  <p>[합성] K-Startup 공고 본문</p>
  <div class="board_file"><ul>
    <li class="clear">
      <a class="file_bg" title="[첨부파일] 공고문.pdf">공고문.pdf</a>
      <div class="btn_wrap"><ul><li>
        <a href="/afile/fileDownload/AAAAA" class="btn_down"><span>다운로드</span></a>
      </li></ul></div>
    </li>
    <li class="clear">
      <a class="file_bg" title="[첨부파일] 신청양식.hwp">신청양식.hwp</a>
      <div class="btn_wrap"><ul><li>
        <a href="/afile/fileDownload/BBBBB" class="btn_down"><span>다운로드</span></a>
      </li></ul></div>
    </li>
  </ul></div>
</div>
<div class="footer_area"><footer>푸터</footer></div>
</body></html>
"""


class FakeResp:
    def __init__(self, data, url):
        self._data, self._pos, self._url = data, 0, url
        self.headers = email.message.Message()

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


BIZ_URL = "https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId=PBLN_000000000001"


def write_biz_jsonl(tmp_path):
    p = tmp_path / "bizinfo.jsonl"
    p.write_text(json.dumps({
        "source": "bizinfo", "id": "PBLN_000000000001", "title": "합성",
        "url": BIZ_URL,
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def test_bizinfo_download_all_ok_stamps_hash_v3(sources_crawl, attach_download,
                                                monkeypatch, tmp_path):
    """robots 불허 첨부가 없고 전부 다운로드 성공 → hash v3 + complete + exit 0."""
    html = BIZ_DETAIL_HTML.replace(
        '<a href="/uploads/legacy/양식.hwp">양식.hwp</a>', "")
    data = b"%PDF synthetic"
    monkeypatch.setattr(attach_download, "_urlopen",
                        lambda req, timeout: FakeResp(data, req.full_url))
    jsonl = write_biz_jsonl(tmp_path)
    sources_crawl.cmd_detail(
        lambda url, data=None: (200, html), [BIZ_URL], str(tmp_path / "details"),
        download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    assert rec["attachments_complete"] is True
    assert rec["hash_version"] == 3
    (att,) = rec["attachments"]
    assert att["download_status"] == "ok"
    assert att["sha256"] == hashlib.sha256(data).hexdigest()
    # v3 산식 재현: 본문 + 정렬된 첨부 sha256
    body = sources_crawl.extract_body(html)
    assert rec["content_hash"] == attach_download.content_hash_of(
        body, [att["sha256"]])


def test_bizinfo_robots_attachment_keeps_v2_and_exit2(sources_crawl, attach_download,
                                                      monkeypatch, tmp_path):
    """/uploads/ 첨부는 robots 불허 → skipped_robots(링크만), 본문 v2 유지 +
    attachments_complete:false + exit 2. /cmm/fms/ 첨부는 정상 다운로드."""
    data = b"%PDF ok"
    requested = []

    def fake_urlopen(req, timeout):
        requested.append(req.full_url)
        return FakeResp(data, req.full_url)

    monkeypatch.setattr(attach_download, "_urlopen", fake_urlopen)
    jsonl = write_biz_jsonl(tmp_path)
    with pytest.raises(SystemExit) as e:
        sources_crawl.cmd_detail(
            lambda url, data=None: (200, BIZ_DETAIL_HTML), [BIZ_URL],
            str(tmp_path / "details"),
            download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    assert e.value.code == 2
    assert not any("/uploads/" in u for u in requested)  # robots 우회 없음
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    assert rec["attachments_complete"] is False
    assert rec["hash_version"] == 2  # 불완전 → 본문 v2 유지 (None 아님)
    body = sources_crawl.extract_body(BIZ_DETAIL_HTML)
    assert rec["content_hash"] == hashlib.sha256(body.encode()).hexdigest()
    by_status = sorted(a["download_status"] for a in rec["attachments"])
    assert by_status == ["ok", "skipped_robots"]


def test_bizinfo_download_failure_keeps_v2_and_exit2(sources_crawl, attach_download,
                                                     monkeypatch, tmp_path):
    """다운로드 실패(HTTP 500) → failed, v2 유지, exit 2."""
    import io
    import urllib.error

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 500, "boom",
                                     email.message.Message(), io.BytesIO(b""))

    html = BIZ_DETAIL_HTML.replace(
        '<a href="/uploads/legacy/양식.hwp">양식.hwp</a>', "")
    monkeypatch.setattr(attach_download, "_urlopen", fake_urlopen)
    jsonl = write_biz_jsonl(tmp_path)
    with pytest.raises(SystemExit) as e:
        sources_crawl.cmd_detail(
            lambda url, data=None: (200, html), [BIZ_URL],
            str(tmp_path / "details"),
            download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    assert e.value.code == 2
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    assert rec["attachments"][0]["download_status"] == "failed"
    assert rec["hash_version"] == 2
    assert rec["attachments_complete"] is False


def test_bizinfo_blocked_redirect_attachment_exit2(sources_crawl, attach_download,
                                                   monkeypatch, tmp_path):
    """첨부가 외부 호스트로 302 → 요청 전 차단(blocked_redirect), exit 2."""
    import io
    import urllib.error
    requested = []

    def fake_urlopen(req, timeout):
        requested.append(req.full_url)
        hdrs = email.message.Message()
        hdrs["Location"] = "https://evil.example/malware.exe"
        raise urllib.error.HTTPError(req.full_url, 302, "moved", hdrs,
                                     io.BytesIO(b""))

    html = BIZ_DETAIL_HTML.replace(
        '<a href="/uploads/legacy/양식.hwp">양식.hwp</a>', "")
    monkeypatch.setattr(attach_download, "_urlopen", fake_urlopen)
    jsonl = write_biz_jsonl(tmp_path)
    with pytest.raises(SystemExit) as e:
        sources_crawl.cmd_detail(
            lambda url, data=None: (200, html), [BIZ_URL],
            str(tmp_path / "details"),
            download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    assert e.value.code == 2
    assert not any("evil.example" in u for u in requested)  # 외부 요청 없음
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    assert rec["attachments"][0]["download_status"] == "blocked_redirect"
    assert rec["hash_version"] == 2


def test_bizinfo_no_attachments_complete_v2(sources_crawl, attach_download,
                                            monkeypatch, tmp_path):
    """첨부가 없으면 링크만으로 complete=True, 본문 v2, exit 0."""
    html = "<div class='view_cont'><p>본문</p></div><div id='footer'>f</div>"

    def boom(req, timeout):
        raise AssertionError("첨부가 없는데 다운로드 요청이 나갔다")

    monkeypatch.setattr(attach_download, "_urlopen", boom)
    jsonl = write_biz_jsonl(tmp_path)
    sources_crawl.cmd_detail(
        lambda url, data=None: (200, html), [BIZ_URL], str(tmp_path / "details"),
        download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    assert rec["attachments"] == []
    assert rec["attachments_complete"] is True
    assert rec["hash_version"] == 2


def test_detail_without_flags_keeps_legacy_format(sources_crawl, tmp_path):
    """--download-dir/--merge-into 미지정 시 기존 포맷(헤더 없음) 그대로."""
    outdir = tmp_path / "details"
    sources_crawl.cmd_detail(
        lambda url, data=None: (200, BIZ_DETAIL_HTML), [BIZ_URL], str(outdir))
    (f,) = list(outdir.iterdir())
    text = f.read_text(encoding="utf-8")
    assert "CONTENT_HASH:" not in text
    assert text.startswith(BIZ_URL + "\n\n")


# ------------------------------------------------------------- K-Startup

def test_kstartup_parse_attachments_contract(kstartup_crawl):
    atts = kstartup_crawl.parse_attachments(KS_DETAIL_HTML)
    assert [a["filename"] for a in atts] == ["공고문.pdf", "신청양식.hwp"]
    assert atts[0]["url"] == "https://www.k-startup.go.kr/afile/fileDownload/AAAAA"
    assert atts[1]["url"].endswith("/BBBBB")


def test_kstartup_afile_is_robots_disallowed(kstartup_crawl, attach_download):
    """실측(2026-07-23): 첨부 다운로드 경로 /afile은 robots 불허 —
    다운로드 금지가 접두 목록으로 강제된다."""
    assert not attach_download.robots_allowed(
        "https://www.k-startup.go.kr/afile/fileDownload/AAAAA",
        kstartup_crawl.KSTARTUP_ROBOTS_DISALLOWED)


def test_kstartup_detail_attachments_links_only_v2_exit2(
        kstartup_crawl, attach_download, monkeypatch, tmp_path):
    """K-Startup 첨부는 robots 불허 → 전건 skipped_robots(링크만),
    본문 v2 유지 + attachments_complete:false + exit 2. 요청 0건."""
    def boom(req, timeout):
        raise AssertionError("robots 불허 /afile에 요청이 나갔다")

    monkeypatch.setattr(attach_download, "_urlopen", boom)
    monkeypatch.setattr(kstartup_crawl, "make_fetcher",
                        lambda: (lambda url: (200, KS_DETAIL_HTML), "fake"))
    jsonl = tmp_path / "kstartup_all.jsonl"
    jsonl.write_text(json.dumps({"pbancSn": "178481", "title": "합성"},
                                ensure_ascii=False) + "\n", encoding="utf-8")
    args = types.SimpleNamespace(
        pbancSn=["178481"], output=str(tmp_path / "details"),
        download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    with pytest.raises(SystemExit) as e:
        kstartup_crawl.cmd_detail(args)
    assert e.value.code == 2
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    assert rec["attachments_complete"] is False
    assert rec["hash_version"] == 2
    assert all(a["download_status"] == "skipped_robots"
               for a in rec["attachments"])
    assert len(rec["attachments"]) == 2
    import hashlib as _h
    body = kstartup_crawl.extract_body(KS_DETAIL_HTML)
    assert rec["content_hash"] == _h.sha256(body.encode()).hexdigest()
    # 상세 txt에도 계약 헤더가 남는다
    text = (tmp_path / "details" / "178481.txt").read_text(encoding="utf-8")
    assert "HASH_VERSION: 2" in text
    assert "skipped_robots" in text


def test_kstartup_detail_no_attachments_complete_v2(kstartup_crawl, attach_download,
                                                    monkeypatch, tmp_path):
    html = "<div class='content_wrap'><p>본문만</p></div><footer>f</footer>"
    monkeypatch.setattr(kstartup_crawl, "make_fetcher",
                        lambda: (lambda url: (200, html), "fake"))
    jsonl = tmp_path / "kstartup_all.jsonl"
    jsonl.write_text(json.dumps({"pbancSn": "1", "title": "합성"}) + "\n",
                     encoding="utf-8")
    args = types.SimpleNamespace(
        pbancSn=["1"], output=str(tmp_path / "details"),
        download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    kstartup_crawl.cmd_detail(args)  # exit 0 → no SystemExit
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    assert rec["attachments_complete"] is True
    assert rec["hash_version"] == 2


def test_kstartup_detail_legacy_format_untouched(kstartup_crawl, monkeypatch,
                                                 tmp_path):
    monkeypatch.setattr(kstartup_crawl, "make_fetcher",
                        lambda: (lambda url: (200, KS_DETAIL_HTML), "fake"))
    args = types.SimpleNamespace(pbancSn=["7"], output=str(tmp_path / "details"),
                                 download_dir=None, merge_into=None)
    kstartup_crawl.cmd_detail(args)
    text = (tmp_path / "details" / "7.txt").read_text(encoding="utf-8")
    assert "CONTENT_HASH:" not in text


# ---- 401/403 시에도 v2/incomplete 병합 (Codex 게이트 #4 회귀) -----------------

def test_bizinfo_403_attachment_still_merges_v2_incomplete(
        sources_crawl, attach_download, monkeypatch, tmp_path):
    """첨부 403(ManualEscalation) 시에도 merge_detail이 실행돼 재시도 파일의
    과거 v3/attachments_complete:true가 v2/false로 교체된다. 차단 신호는
    exit 3(MANUAL)로 전파된다."""
    import io
    import urllib.error

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden",
                                     email.message.Message(), io.BytesIO(b""))

    html = BIZ_DETAIL_HTML.replace(
        '<a href="/uploads/legacy/양식.hwp">양식.hwp</a>', "")
    monkeypatch.setattr(attach_download, "_urlopen", fake_urlopen)
    # 재시도 시나리오: 직전 런이 남긴 v3/complete:true 레코드
    jsonl = tmp_path / "bizinfo.jsonl"
    jsonl.write_text(json.dumps({
        "source": "bizinfo", "id": "PBLN_000000000001", "title": "합성",
        "url": BIZ_URL, "content_hash": "OLD_V3_HASH", "hash_version": 3,
        "attachments_complete": True,
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        sources_crawl.cmd_detail(
            lambda url, data=None: (200, html), [BIZ_URL],
            str(tmp_path / "details"),
            download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    assert e.value.code == 3  # MANUAL(차단 신호)
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    assert rec["attachments_complete"] is False  # 과거 true 잔존 금지
    assert rec["hash_version"] == 2              # 과거 v3 잔존 금지
    assert rec["content_hash"] != "OLD_V3_HASH"
    assert rec["attachments"][0]["download_status"] == "failed"
    assert "manual" in rec["attachments"][0]["download_reason"]


def test_kstartup_403_attachment_still_merges_v2_incomplete(
        kstartup_crawl, attach_download, monkeypatch, tmp_path):
    """kstartup 경로도 동일 — ManualEscalation이 병합을 건너뛰지 않는다.
    (robots 전건 skip이 기본이지만, 접두 목록 변경 등으로 다운로드가 시도될
    때의 계약을 고정한다.)"""
    import io
    import urllib.error

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden",
                                     email.message.Message(), io.BytesIO(b""))

    monkeypatch.setattr(attach_download, "_urlopen", fake_urlopen)
    # robots 접두를 비워 다운로드 시도를 강제 (계약 고정용 합성 시나리오)
    monkeypatch.setattr(kstartup_crawl, "KSTARTUP_ROBOTS_DISALLOWED", ())
    monkeypatch.setattr(kstartup_crawl, "make_fetcher",
                        lambda: (lambda url: (200, KS_DETAIL_HTML), "fake"))
    jsonl = tmp_path / "kstartup_all.jsonl"
    jsonl.write_text(json.dumps({
        "pbancSn": "178481", "title": "합성",
        "content_hash": "OLD", "hash_version": 3, "attachments_complete": True,
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    args = types.SimpleNamespace(
        pbancSn=["178481"], output=str(tmp_path / "details"),
        download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    with pytest.raises(SystemExit) as e:
        kstartup_crawl.cmd_detail(args)
    assert e.value.code == 3  # MANUAL(차단 신호)
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    assert rec["attachments_complete"] is False
    assert rec["hash_version"] == 2
