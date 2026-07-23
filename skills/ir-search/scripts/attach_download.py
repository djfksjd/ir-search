#!/usr/bin/env python3
"""첨부 다운로드 공용 모듈 — ir-search detail 크롤러 (hash v2/v3).

sole-search의 검증된 bizinfo 첨부 슬라이스(2026-07-23 실측)를 이식했다.
run_manifest.py처럼 두 크롤러(sources_crawl.py, kstartup_crawl.py)가 공유한다.

보안 계약:
  - 모든 요청은 자동 리다이렉트를 끈 opener로 보낸다(_NoRedirect).
  - 각 Location을 **요청을 보내기 전에** 절대 URL로 해석해 https+허용 호스트
    검사를 통과할 때만 최대 5홉(MAX_REDIRECTS) 수동 추적한다. 위반 시
    RedirectBlocked — 외부 호스트로는 요청 자체가 나가지 않는다.
  - 첨부는 50MB 스트리밍 상한(MAX_ATTACH_BYTES). 초과·실패 시 부분 파일 삭제.
  - Content-Disposition 파일명은 latin-1→UTF-8 모지바케를 복구하고
    basename + 문자 정제(safe_filename) + commonpath 검사로만 저장한다
    (경로 탈출·심볼릭 링크 차단).
  - robots.txt 불허 경로는 다운로드하지 않고 링크만 남긴다
    (download_status "skipped_robots") — robots 우회 금지.

hash 계약:
  HASH_VERSION_BODY(2)   = 본문 텍스트만의 sha256
  HASH_VERSION_ATTACH(3) = 본문 + 정렬된 첨부 sha256 (content_hash_of —
                           sole-search sbiz_crawl.content_hash_of와 동일 산식)
  첨부가 **전부** 다운로드 성공("ok")일 때만 v3를 스탬프한다. 하나라도
  실패·차단·robots 생략이면 본문만의 v2 해시를 유지하고
  attachments_complete:false + exit 2(partial)로 표현한다 — 해시를 None으로
  지우면 반복 실패 두 런 사이의 본문 변경이 diff에서 숨기 때문.
"""
import hashlib
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_REDIRECTS = 5
_REDIRECT_CODES = (301, 302, 303, 307, 308)
MAX_ATTACH_BYTES = 50 * 1024 * 1024  # 첨부 다운로드 상한 50MB (sole-search와 동일)
HASH_VERSION_BODY = 2
HASH_VERSION_ATTACH = 3
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15")


class ManualEscalation(RuntimeError):
    """401/403 — 우회하지 않고 수동 확인으로 전환하라는 신호."""


class RedirectBlocked(RuntimeError):
    """리다이렉트 대상이 https+허용 호스트 검사를 통과하지 못함 — 요청 전에 차단."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """자동 리다이렉트 금지 — open_validated가 각 Location을 요청 전에 검증한다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# 전역 opener를 오염시키지 않는다 — 이 모듈 전용 opener만 리다이렉트를 끈다.
_opener = urllib.request.build_opener(_NoRedirect())


def _urlopen(req, timeout):
    """테스트가 monkeypatch하는 단일 통로 — 실제 소켓은 여기서만 열린다."""
    return _opener.open(req, timeout=timeout)


def host_allowed(url, allowed_hosts):
    """https + 정확한 호스트/서브도메인 경계 검사 — endswith/부분 문자열 매칭은
    evilbizinfo.go.kr, userinfo(@)·쿼리스트링 위장에 뚫린다."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in allowed_hosts)


def open_validated(url, allowed_hosts, timeout, robots_disallowed=()):
    """자동 리다이렉트 없이 열고, 각 Location을 **요청을 보내기 전에** 절대 URL로
    해석해 https+허용 호스트 + robots 불허 접두(경로) 검사를 통과할 때만 최대
    5홉 수동 추적한다. 위반 시 RedirectBlocked — 외부 호스트로도, robots 불허
    경로로도 요청 자체가 나가지 않는다 (예: /cmm/fms/ → 302 → /uploads/…
    같은 동일 호스트 리다이렉트로 robots를 우회할 수 없다)."""
    if not host_allowed(url, allowed_hosts):
        raise RedirectBlocked(f"URL host/scheme 불허: {url[:80]}")
    if not robots_allowed(url, robots_disallowed):
        raise RedirectBlocked(f"robots 불허 경로 — 요청 차단: {url[:80]}")
    for _ in range(MAX_REDIRECTS + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            return _urlopen(req, timeout)
        except urllib.error.HTTPError as e:
            if e.code not in _REDIRECT_CODES:
                raise
            loc = e.headers.get("Location") if e.headers else None
            e.close()
            if not loc:
                raise RedirectBlocked(f"리다이렉트 Location 없음: {url[:80]}")
            nxt = urllib.parse.urljoin(url, loc)
            if not host_allowed(nxt, allowed_hosts):
                raise RedirectBlocked(f"리다이렉트 대상 불허 — 요청 차단: {nxt[:80]}")
            if not robots_allowed(nxt, robots_disallowed):
                raise RedirectBlocked(
                    f"리다이렉트 대상이 robots 불허 경로 — 요청 차단: {nxt[:80]}")
            url = nxt
    raise RedirectBlocked(f"리다이렉트 {MAX_REDIRECTS}홉 초과: {url[:80]}")


def content_hash_of(body_text, attachment_hashes):
    """hash v3 산식 — sole-search sbiz_crawl.content_hash_of와 동일해야 한다."""
    payload = body_text + "\n" + "\n".join(sorted(attachment_hashes))
    return hashlib.sha256(payload.encode()).hexdigest()


def safe_filename(name, idx):
    """서버 제공 파일명을 신뢰하지 않는다 — basename + 문자 정제 + 순번 프리픽스."""
    base = re.sub(r"[^\w.\-가-힣()\[\] ]", "_",
                  (name or "").replace("\\", "/").rsplit("/", 1)[-1])
    return f"{idx:02d}_{base[:120]}" if base else f"{idx:02d}_attach"


_MAX_UNQUOTE = 5  # 반복 percent-디코딩 상한 (이중 인코딩 %2575… 커버)


def robots_allowed(url, disallowed_prefixes):
    """robots.txt 불허 접두 경로 검사 — 매칭되면 다운로드 금지(링크만 수집).

    fail-closed: percent-인코딩 위장(/%75ploads/, 이중 인코딩 /%2575ploads/)을
    반복 unquote(최대 5회, 고정점 도달 시 중단)로 정규화하고, **원본과 모든
    디코딩 단계 + normpath 정규화 형태 중 하나라도** 불허 접두에 걸리면
    거부한다. 디코딩 불가/파싱 불가도 거부."""
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        return False
    candidates = []
    cur = path
    for _ in range(_MAX_UNQUOTE + 1):
        candidates.append(cur)
        try:
            nxt = urllib.parse.unquote(cur)
        except (ValueError, UnicodeDecodeError):
            return False  # 디코딩 불가 — fail-closed
        if nxt == cur:
            break
        cur = nxt
    else:
        return False  # 상한 내 고정점 미도달(과도한 다중 인코딩) — fail-closed
    for c in list(candidates):
        # /a/../uploads 류 경로 정규화 형태도 함께 검사
        candidates.append(os.path.normpath(c))
    return not any(c.startswith(p) for c in candidates for p in disallowed_prefixes)


def recover_filename(cd_name):
    """Content-Disposition 파일명 복구: 서버가 UTF-8 바이트를 그대로 보내면
    latin-1로 잘못 디코드된 모지바케가 온다 — 되돌려서 복원
    (실측: bizinfo, 2026-07-23). %-인코딩도 unquote한다."""
    if not cd_name:
        return cd_name
    try:
        cd_name = cd_name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    if "%" in cd_name:
        cd_name = urllib.parse.unquote(cd_name)
    return cd_name


def download_attachment(url, dirpath, fallback_name, idx, allowed_hosts,
                        robots_disallowed=()):
    """보안 계약: 요청 전 host_allowed(https 강제 포함) + 각 리다이렉트 Location을
    **요청 전에** 검증(open_validated — 허용 호스트와 robots 불허 접두 모두,
    위반 시 RedirectBlocked) + 50MB 스트리밍 상한 + 실패 시 부분 파일 삭제.
    저장 경로를 반환한다."""
    if not host_allowed(url, allowed_hosts):
        raise RuntimeError(f"첨부 URL host/scheme 불허: {url[:80]}")
    dirpath = str(pathlib.Path(dirpath).resolve())
    path = None
    try:
        with open_validated(url, allowed_hosts, timeout=60,
                            robots_disallowed=robots_disallowed) as r:
            # 사전 검증이 1차 방어 — geturl 재검사는 심층 방어로 유지한다
            final = r.geturl() if hasattr(r, "geturl") else url
            if not host_allowed(final, allowed_hosts):
                raise RuntimeError(f"리다이렉트 최종 URL host 불허: {final[:80]}")
            length = r.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_ATTACH_BYTES:
                raise RuntimeError(f"첨부 Content-Length가 상한 초과: {length}")
            cd_name = recover_filename(r.headers.get_filename())
            path = (pathlib.Path(dirpath) /
                    safe_filename(cd_name or fallback_name, idx)).resolve()
            if os.path.commonpath([str(path), dirpath]) != dirpath \
                    or path.is_symlink():
                raise RuntimeError("path_escape_blocked")
            read = 0
            with open(path, "wb") as fh:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    read += len(chunk)
                    if read > MAX_ATTACH_BYTES:
                        raise RuntimeError(
                            f"첨부가 {MAX_ATTACH_BYTES // (1 << 20)}MB 상한 초과")
                    fh.write(chunk)
            return path
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ManualEscalation(f"첨부 다운로드 HTTP {e.code}") from e
        raise
    except (RuntimeError, OSError):
        if path is not None:
            path.unlink(missing_ok=True)  # 부분 파일 잔존 방지
        raise


def safe_subdir(name):
    """공고 식별자를 하위 폴더명으로 정제 — 경로 구분자·제어문자 제거."""
    return re.sub(r"[^\w.\-가-힣]", "_", str(name))[:80] or "_"


def process_attachments(attachments, download_dir, delay, allowed_hosts,
                        robots_disallowed_prefixes, tag="ir-search",
                        subdir=None):
    """첨부 목록을 다운로드하고 sha256 목록을 반환한다.

    *subdir*(공고 식별자 — pblancId/pbancSn 등)를 주면 download_dir 아래
    공고별 하위 폴더에 저장한다 — 여러 공고의 동명 첨부(00_공고문.pdf 등)가
    서로 덮어쓰는 것을 막는다. local_path는 실제 저장 경로를 기록한다.

    각 항목 dict에 download_status(ok/failed/blocked_redirect/skipped_robots),
    sha256, local_path를 기록한다. 텍스트 추출은 이 라운드 범위 밖 —
    hash v3 판단은 download_status "ok" 전건 여부만 본다.
    ManualEscalation(401/403)은 그대로 올린다(호출부가 수동 전환 처리)."""
    base = pathlib.Path(download_dir).resolve()
    d = base
    if subdir is not None:
        d = base / safe_subdir(subdir)
        # 사전 배치된 symlink 하위 폴더로 base 밖에 기록되는 탈출 차단:
        # (a) 폴더 자체가 symlink면 거부, (b) 생성 후 realpath가 base 내부인지
        # 재검증 — 파일 최종 경로 검사(download_attachment)도 이 realpath 기준
        # dirpath로 수행된다.
        if d.is_symlink():
            raise RuntimeError(f"subdir_symlink_blocked: {d.name}")
    d.mkdir(parents=True, exist_ok=True)
    real_d = pathlib.Path(os.path.realpath(d))
    real_base = pathlib.Path(os.path.realpath(base))
    if real_d != real_base and \
            os.path.commonpath([str(real_d), str(real_base)]) != str(real_base):
        raise RuntimeError(f"subdir_escape_blocked: {d}")
    d = real_d
    attach_hashes = []
    for idx, f in enumerate(attachments):
        if not robots_allowed(f["url"], robots_disallowed_prefixes):
            f["download_status"] = "skipped_robots"
            print(f"[{tag}] robots 불허 경로 — 다운로드 생략(링크만): "
                  f"{f['url'][:80]}", file=sys.stderr)
            continue
        time.sleep(delay)
        try:
            path = download_attachment(f["url"], d, f.get("filename"), idx,
                                       allowed_hosts,
                                       robots_disallowed=robots_disallowed_prefixes)
        except ManualEscalation:
            raise  # 차단 신호 — 호출부에서 수동 전환
        except RedirectBlocked as e:
            f["download_status"] = "blocked_redirect"
            f["download_reason"] = str(e)
            print(f"WARNING [{tag}] attachment {f.get('filename', '?')}: "
                  f"리다이렉트 차단 — {e}", file=sys.stderr)
            continue
        except (urllib.error.URLError, urllib.error.HTTPError,
                RuntimeError, OSError, TimeoutError) as e:
            f["download_status"] = "failed"
            f["download_reason"] = str(e)
            print(f"WARNING [{tag}] attachment {f.get('filename', '?')}: {e}",
                  file=sys.stderr)
            continue
        f["local_path"] = str(path)
        f["filename"] = path.name
        f["download_status"] = "ok"
        f["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        attach_hashes.append(f["sha256"])
    return attach_hashes
