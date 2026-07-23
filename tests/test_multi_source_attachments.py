"""NIPA·KOCCA·SMTECH 첨부 슬라이스 테스트 (no network, 합성 fixture).

실측(2026-07-24) 계약 기반: references/sources.md 참조.
"""
import email.message
import hashlib
import json

import pytest

NIPA_DETAIL_HTML = """
<html><body>
<div class="tbWrap gonggo detail cf"><table><tbody>
<tr><td>공고내용 본문</td></tr>
<tr><td class="tc bg_lightgray">첨부파일</td><td colspan="6">
  <a href="/comm/getFile?srvcId=BBSTY1&amp;upperNo=AA==&amp;fileTy=ATTACH&amp;fileNo=BB==">
      붙임_1._사업_공고문.hwp (파일크기: 134 KB<!-- 주석 -->)
  </a><br/>
  <a href="/comm/getFile?srvcId=BBSTY1&amp;upperNo=CC==&amp;fileTy=ATTACH&amp;fileNo=DD==">
      붙임_2._신청서.hwp (파일크기: 90 KB)
  </a>
</td></tr>
</tbody></table></div>
<footer id="footer">푸터</footer>
</body></html>
"""

SMTECH_DETAIL_HTML = """
<html><body>
<div id="subcontent">
<p>SMTECH 공고 본문</p>
<p class="fl"><a href="#list" onclick="cfn_AtchFileDownload('5CF6AF12AAAA','/front','fileDownFrame'); return false;" class="pop">(서식3) 생산설비 소개서.hwp</a></p>
<p class="fl"><a href="javascript:cfn_AtchFileDownload('5CF6AF12BBBB','/front','fileDownFrame');">(공고문) 지원사업.pdf</a></p>
<p class="fl"><a href="#list" onclick="cfn_AtchFileDownload('5CF6AF12AAAA','/front','fileDownFrame'); return false;">(서식3) 생산설비 소개서.hwp</a></p>
</div>
<div id="footer">푸터</div>
</body></html>
"""

KOCCA_DETAIL_HTML = """
<html><body>
<div id="contents_body">
<p>KOCCA 공고 본문</p>
<a href="javascript:openNoticeFileList1('326D00011111')">첨부파일 보기</a>
<a href="javascript:openNoticeFileList2('76JNATPO10LM1AV000')">PMS 첨부</a>
</div>
<footer id="footer">푸터</footer>
</body></html>
"""

KOCCA_POPUP_HTML = """
<html><body><table><tbody>
<tr><td><a href="javascript:fn_fileDownload('326D00011111','1')">공고문.hwp</a></td></tr>
<tr><td><a href="#" onclick="fn_fileDownload('326D00011111', '2')">신청양식.hwp</a></td></tr>
</tbody></table></body></html>
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


# ------------------------------------------------------------ 파서 계약

def test_parse_nipa_attachments_names_and_urls(sources_crawl):
    atts = sources_crawl.parse_nipa_attachments(NIPA_DETAIL_HTML)
    assert [a["filename"] for a in atts] == ["붙임_1._사업_공고문.hwp",
                                             "붙임_2._신청서.hwp"]
    assert atts[0]["url"].startswith("https://www.nipa.kr/comm/getFile?srvcId=")
    assert "&upperNo=AA==" in atts[0]["url"]  # HTML 엔티티 언이스케이프


def test_parse_smtech_attachments_dedup_and_url(sources_crawl):
    atts = sources_crawl.parse_smtech_attachments(SMTECH_DETAIL_HTML)
    assert len(atts) == 2  # 동일 ID(onclick/href 변형) dedupe
    assert atts[0]["url"] == ("https://www.smtech.go.kr/front/comn/"
                              "AtchFileDownload.do?atchFileId=5CF6AF12AAAA")
    assert atts[0]["filename"] == "(서식3) 생산설비 소개서.hwp"
    assert atts[1]["filename"] == "(공고문) 지원사업.pdf"


def test_parse_kocca_attachments_popup1_and_popup2(sources_crawl):
    fetched = []

    def fetch(url, data=None):
        fetched.append(url)
        return 200, KOCCA_POPUP_HTML

    atts = sources_crawl.parse_kocca_attachments(KOCCA_DETAIL_HTML, fetch)
    assert fetched == ["https://www.kocca.kr/kocca/noticeFilePop.do"
                       "?intcNo=326D00011111"]
    dl = [a for a in atts if "noticeFileDown" in a["url"]]
    assert [(a["filename"], a["url"].split("seqNo=")[1]) for a in dl] == [
        ("공고문.hwp", "1"), ("신청양식.hwp", "2")]
    pms = [a for a in atts if "pms.kocca.kr" in a["url"]]
    assert len(pms) == 1
    assert pms[0]["download_status"] == "skipped_unverified"


def test_parse_kocca_popup_failure_yields_no_phantom_files(sources_crawl):
    def fetch(url, data=None):
        return 500, ""

    atts = sources_crawl.parse_kocca_attachments(KOCCA_DETAIL_HTML, fetch)
    assert all("noticeFileDown" not in a["url"] for a in atts)  # 파일 행 없음
    assert any("pms.kocca.kr" in a["url"] for a in atts)


# ------------------------------------------------------ robots 판정 계약

def test_kocca_robots_wildcard_filedown_blocked_but_noticefiledown_allowed(
        sources_crawl, attach_download):
    R = sources_crawl.KOCCA_ROBOTS_DISALLOWED
    assert not attach_download.robots_allowed(
        "https://www.kocca.kr/kocca/bbs/FileDown.do?f=1", R)  # /*/FileDown.do
    assert attach_download.robots_allowed(
        "https://www.kocca.kr/kocca/noticeFileDown.do?intcNo=1&seqNo=1", R)
    assert attach_download.robots_allowed(
        "https://www.kocca.kr/kocca/noticeFilePop.do?intcNo=1", R)
    assert not attach_download.robots_allowed(
        "https://www.kocca.kr/kocca/pims/list.do", R)  # /kocca/*/list.do


def test_smtech_nipa_robots_allow_attachment_endpoints(sources_crawl,
                                                       attach_download):
    assert attach_download.robots_allowed(
        "https://www.smtech.go.kr/front/comn/AtchFileDownload.do?atchFileId=A",
        sources_crawl.SMTECH_ROBOTS_DISALLOWED)
    assert not attach_download.robots_allowed(
        "https://www.smtech.go.kr/nmbi/x", sources_crawl.SMTECH_ROBOTS_DISALLOWED)
    assert attach_download.robots_allowed(
        "https://www.nipa.kr/comm/getFile?x=1",
        sources_crawl.NIPA_ROBOTS_DISALLOWED)


def test_robots_wildcard_end_anchor(attach_download):
    assert attach_download._robots_path_match("/a/b.do", "/a/*.do$")
    assert not attach_download._robots_path_match("/a/b.dox", "/a/*.do$")


# ------------------------------------------------- cmd_detail 통합 (e2e)

NIPA_URL = "https://www.nipa.kr/home/2-2/16866"
SMTECH_URL = ("https://www.smtech.go.kr/front/ifg/no/notice02_detail.do"
              "?buclYy=&ancmId=S02874&buclCd=S9111&dtlAncmSn=1")
KOCCA_URL = "https://www.kocca.kr/kocca/pims/view.do?intcNo=326D00011111&menuNo=204104"


def write_rec(tmp_path, name, rec):
    p = tmp_path / name
    p.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def test_nipa_detail_download_all_ok_hash_v3(sources_crawl, attach_download,
                                             monkeypatch, tmp_path):
    data = b"HWPDATA"
    monkeypatch.setattr(attach_download, "_urlopen",
                        lambda req, timeout: FakeResp(data, req.full_url))
    jsonl = write_rec(tmp_path, "nipa.jsonl",
                      {"source": "nipa", "id": "16866", "title": "합성"})
    sources_crawl.cmd_detail(
        lambda url, data=None: (200, NIPA_DETAIL_HTML), [NIPA_URL],
        str(tmp_path / "details"),
        download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    assert rec["attachments_complete"] is True
    assert rec["hash_version"] == 3
    assert len(rec["attachments"]) == 2
    assert all(a["download_status"] == "ok" for a in rec["attachments"])
    body = sources_crawl.extract_body(
        NIPA_DETAIL_HTML, *sources_crawl.BODY_MARKERS["nipa"])
    assert rec["content_hash"] == attach_download.content_hash_of(
        body, [a["sha256"] for a in rec["attachments"]])
    # 공고별 subdir
    import pathlib
    assert pathlib.Path(rec["attachments"][0]["local_path"]).parent.name == "16866"


def test_smtech_detail_download_all_ok_hash_v3(sources_crawl, attach_download,
                                               monkeypatch, tmp_path):
    monkeypatch.setattr(attach_download, "_urlopen",
                        lambda req, timeout: FakeResp(b"PDF", req.full_url))
    jsonl = write_rec(tmp_path, "smtech.jsonl",
                      {"source": "smtech", "id": "S02874", "title": "합성"})
    sources_crawl.cmd_detail(
        lambda url, data=None: (200, SMTECH_DETAIL_HTML), [SMTECH_URL],
        str(tmp_path / "details"),
        download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    assert rec["attachments_complete"] is True
    assert rec["hash_version"] == 3
    assert len(rec["attachments"]) == 2


def test_kocca_detail_pms_popup_keeps_v2_incomplete_exit2(
        sources_crawl, attach_download, monkeypatch, tmp_path):
    """popup1 파일은 다운로드 성공해도 popup2(pms, 계약 미확정)가 있으면
    skipped_unverified 링크만 → v2 유지 + attachments_complete:false + exit 2.
    사전 마킹 상태는 process_attachments가 덮어쓰지 않는다."""
    requested = []

    def fake_urlopen(req, timeout):
        requested.append(req.full_url)
        return FakeResp(b"FILE", req.full_url)

    monkeypatch.setattr(attach_download, "_urlopen", fake_urlopen)

    def fetch(url, data=None):
        if "noticeFilePop" in url:
            return 200, KOCCA_POPUP_HTML
        return 200, KOCCA_DETAIL_HTML

    jsonl = write_rec(tmp_path, "kocca.jsonl",
                      {"source": "kocca", "id": "326D00011111", "title": "합성"})
    with pytest.raises(SystemExit) as e:
        sources_crawl.cmd_detail(
            fetch, [KOCCA_URL], str(tmp_path / "details"),
            download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    assert e.value.code == 2
    assert not any("pms.kocca.kr" in u for u in requested)  # 미확정 링크 요청 없음
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    assert rec["attachments_complete"] is False
    assert rec["hash_version"] == 2
    statuses = sorted(a["download_status"] for a in rec["attachments"])
    assert statuses == ["ok", "ok", "skipped_unverified"]
    ok = [a for a in rec["attachments"] if a["download_status"] == "ok"]
    assert all(a["sha256"] == hashlib.sha256(b"FILE").hexdigest() for a in ok)


def test_kocca_detail_popup1_only_all_ok_hash_v3(sources_crawl, attach_download,
                                                 monkeypatch, tmp_path):
    """popup2가 없고 popup1 파일 전부 성공 → hash v3 + complete."""
    html = KOCCA_DETAIL_HTML.replace(
        "<a href=\"javascript:openNoticeFileList2('76JNATPO10LM1AV000')\">"
        "PMS 첨부</a>", "")
    monkeypatch.setattr(attach_download, "_urlopen",
                        lambda req, timeout: FakeResp(b"FILE", req.full_url))

    def fetch(url, data=None):
        return (200, KOCCA_POPUP_HTML) if "noticeFilePop" in url else (200, html)

    jsonl = write_rec(tmp_path, "kocca.jsonl",
                      {"source": "kocca", "id": "326D00011111", "title": "합성"})
    sources_crawl.cmd_detail(
        fetch, [KOCCA_URL], str(tmp_path / "details"),
        download_dir=str(tmp_path / "atts"), merge_into=str(jsonl))
    rec = json.loads(jsonl.read_text(encoding="utf-8"))
    assert rec["attachments_complete"] is True
    assert rec["hash_version"] == 3


def test_premarked_status_preserved_by_process_attachments(attach_download,
                                                           monkeypatch, tmp_path):
    def boom(req, timeout):
        raise AssertionError("사전 마킹 항목이 요청됐다")

    monkeypatch.setattr(attach_download, "_urlopen", boom)
    atts = [{"url": "https://pms.kocca.kr/pblanc/x", "filename": None,
             "download_status": "skipped_unverified"}]
    hashes = attach_download.process_attachments(
        atts, tmp_path, 0, ("kocca.kr",), ())
    assert hashes == []
    assert atts[0]["download_status"] == "skipped_unverified"
