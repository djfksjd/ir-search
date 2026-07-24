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
import html
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
    evilbizinfo.go.kr, userinfo(@)·쿼리스트링 위장에 뚫린다.

    허용 항목이 '='로 시작하면 **정확한 호스트 일치만** 허용한다(서브도메인
    매칭 배제). 예: '=www.kocca.kr'은 www.kocca.kr만 통과 — pms.kocca.kr 등
    계약 미확정 서브도메인으로의 리다이렉트가 요청되지 않는다."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    for a in allowed_hosts:
        if a.startswith("="):
            if host == a[1:]:
                return True
        elif host == a or host.endswith("." + a):
            return True
    return False


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


# 첨부로 위장한 HTML 오류/차단 페이지 감지 — HTML 첨부(.html)가 아닌데 본문이
# HTML 태그로 시작하면 200 위장 소프트 차단(세션만료·CAPTCHA)으로 본다(Codex ir #1).
# 파일명이 URL 기반(getFile.do 등)이라 확장자가 없을 수 있으므로 확장자 화이트리스트
# 방식: .html/.htm 첨부만 예외로 허용하고 나머지는 HTML 바이트를 거부한다.
# HTTP 200으로 위장한 소프트 차단(CAPTCHA·접근거부) 마커 — 상세 페이지 HTML을
# 정상 공고로 해시/병합하면 잘못된 UNCHANGED가 된다(Codex ir #1). sources·kstartup 공용.
_BLOCK_MARKERS = (
    "captcha", "recaptcha", "verify you are human", "are you a robot",
    "access denied", "forbidden", "접근이 제한", "접근이 차단", "비정상적인 접근",
    "자동입력 방지", "로봇이 아닙", "일시적으로 차단",
)


def looks_blocked(html):
    """앞부분에 차단/캡차 마커가 있으면 True — 200 위장 소프트 차단 감지."""
    low = (html or "")[:4000].lower()
    return any(m in low for m in _BLOCK_MARKERS)


def _looks_like_html_error(first_bytes, content_type, filename):
    """마크업/HTML 확장자 첨부가 아닌데 본문이 `<`(태그)로 시작하면 True.
    PDF(%PDF-)·OLE(D0CF)·ZIP/HWPX(PK)·이미지 등 실제 문서 바이너리는 어느 것도
    '<'로 시작하지 않으므로, `<`로 시작하면 HTML/XML 오류·차단 페이지다. 이렇게
    하면 `<!doctype>`뿐 아니라 `<body>`·`<!--`·`<html`·BOM 선행까지 모두 잡힌다
    (Codex ir #1 후속). Content-Type은 서버가 자주 오기재하므로 근거로 쓰지 않는다."""
    name = (filename or "").lower()
    if name.endswith((".html", ".htm", ".xhtml", ".xml", ".svg", ".xsl")):
        return False  # 마크업 첨부 자체는 정상
    head = first_bytes[:512]
    if head[:3] == b"\xef\xbb\xbf":  # UTF-8 BOM 선행 제거
        head = head[3:]
    return head.lstrip()[:1] == b"<"


_MAX_UNQUOTE = 5  # 반복 percent-디코딩 상한 (이중 인코딩 %2575… 커버)
_MAX_UNESCAPE = 5  # 반복 HTML 엔티티 디코딩 상한 (&amp;amp;amp; 다중 인코딩 커버)


def _robots_path_match(path, pattern):
    """robots 패턴 1건 매칭 — 접두 매칭 + 구글 확장 문법('*' 와일드카드,
    '$' 끝 앵커) 지원. KOCCA의 'Disallow:/*/FileDown.do' 같은 패턴용."""
    if "*" not in pattern and not pattern.endswith("$"):
        return path.startswith(pattern)
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern)
    return re.match(regex + ("$" if anchored else ""), path) is not None


def robots_allowed(url, disallowed_prefixes):
    """robots.txt 불허 접두 경로 검사 — 매칭되면 다운로드 금지(링크만 수집).

    fail-closed: percent-인코딩 위장(/%75ploads/, 이중 인코딩 /%2575ploads/)을
    반복 unquote(최대 5회, 고정점 도달 시 중단)로 정규화하고, **원본과 모든
    디코딩 단계 + normpath 정규화 형태 중 하나라도** 불허 접두에 걸리면
    거부한다. 디코딩 불가/파싱 불가도 거부.

    매칭 대상은 path에 쿼리를 결합한 문자열(path?query)이다 — SMTECH의
    'Disallow: /...List.do?RECH_ANCM_ID=S20131' 같은 쿼리 포함 규칙도
    매칭된다. 인코딩 위장 후보 생성도 동일 결합 문자열 기준."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    target = parts.path + (("?" + parts.query) if parts.query else "")
    candidates = []
    cur = target
    for _ in range(_MAX_UNQUOTE + 1):
        # 잘못된 percent 인코딩(%ZZ, 뒤 2자리 non-hex, % 단독)은 unquote가 예외 없이
        # 원문 유지 → robots 우회 소지. **매 디코드 단계**에서 검사한다 — %25ZZ가
        # 1회 디코드되면 %ZZ가 되므로 루프 밖 1회 검사로는 못 잡는다(Codex ir #7 후속).
        if re.search(r"%(?![0-9A-Fa-f]{2})", cur):
            return False
        candidates.append(cur)
        try:
            # errors="strict": 기본값 "replace"는 %FF·%ZZ·절단 %E0%A4를 대체문자로
            # 삼켜 '디코딩 불가 → 거부' 경로를 죽인다(Codex ir #7). strict면 잘못된
            # 바이트가 예외 → fail-closed. 정상 UTF-8 인코딩 경로는 영향 없음.
            nxt = urllib.parse.unquote(cur, errors="strict")
        except (ValueError, UnicodeDecodeError):
            return False  # 디코딩 불가 — fail-closed
        if nxt == cur:
            break
        cur = nxt
    else:
        return False  # 상한 내 고정점 미도달(과도한 다중 인코딩) — fail-closed
    for c in list(candidates):
        # /a/../uploads 류 경로 정규화 형태도 함께 검사 (normpath는 path
        # 부분에만 적용 — 쿼리는 그대로 재결합).
        # POSIX normpath는 선행 '//'를 보존한다 — /%2Fuploads가 디코딩 후
        # //uploads로 남아 startswith(/uploads)를 피하므로, 선행 슬래시를
        # 단일화한 후보도 추가한다
        p_, sep, q_ = c.partition("?")
        n = os.path.normpath(p_) + sep + q_
        candidates.extend([n, re.sub(r"^/+", "/", c), re.sub(r"^/+", "/", n)])
    return not any(_robots_path_match(c, p)
                   for c in candidates for p in disallowed_prefixes)


def _unescape_fixed_point(s):
    """다중 인코딩 HTML 엔티티(&amp;amp;amp;)를 고정점까지 반복 디코드."""
    for _ in range(_MAX_UNESCAPE):
        un = html.unescape(s)
        if un == s:
            return s
        s = un
    return s


def filename_from_content_disposition(cd_header):
    """원시 Content-Disposition 헤더에서 filename 값을 추출한다.

    email 파서의 get_filename()은 **따옴표 없는** 값에서 `&amp;` 내부 `;`를
    파라미터 구분자로 오인해 자른다(실측 SMTECH 2026-07-24):
      `filename=2026³â R&amp;D.hwp` → `2026³â R&amp` (`;D.hwp` 소실).
    그래서 원시 헤더를 직접 파싱한다:
      1. RFC 6266 확장 `filename*=charset'lang'pct-encoded` 우선.
      2. 따옴표로 감싼 `filename="..."`는 그 안을 그대로.
      3. 따옴표 없는 `filename=`는 **엔티티를 먼저 고정점까지 디코드해**
         `&amp;` 내부 `;`를 없앤 뒤 남은 첫 `;`까지를 값으로 본다 —
         파라미터 구분 `;`만 남으므로 파일명 전체가 살아남는다.
    latin-1 모지바케 바이트는 그대로 둔 채(값 문자열만 추출) recover_filename이
    이후 인코딩을 복구한다. 파싱 실패 시 None."""
    if not cd_header:
        return None
    m = re.search(r"filename\*\s*=\s*([^;]+)", cd_header, re.IGNORECASE)
    if m:
        parts = m.group(1).strip().split("'", 2)
        if len(parts) == 3:
            charset, _lang, enc = parts
            try:
                return urllib.parse.unquote(
                    enc, encoding=charset or "utf-8", errors="strict")
            except (LookupError, UnicodeDecodeError, ValueError):
                pass  # 확장 파싱 실패 — 일반 filename= 폴백
    m = re.search(r'filename\s*=\s*"([^"]*)"', cd_header, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"filename\s*=\s*(.+)$", cd_header, re.IGNORECASE)
    if not m:
        return None
    # 엔티티를 먼저 디코드(&amp;→&)해 파라미터 구분 ';' 오인을 제거한 뒤 절단
    return _unescape_fixed_point(m.group(1)).split(";", 1)[0].strip() or None


def _has_mojibake_run(raw):
    """연속된 high-byte(0x80-0xFF)가 2개 이상이면 True — latin-1로 잘못 흘러온
    멀티바이트 인코딩의 서명이다. UTF-8·CP949 한글은 문자당 2~3바이트라 반드시
    연속 high-byte 런을 남긴다. 반대로 0xB7(·) 하나처럼 **고립된** high-byte는
    정상 문자이므로(예: 'AI·DX.pdf') 재해석하면 안 된다."""
    run = 0
    for b in raw:
        if b >= 0x80:
            run += 1
            if run >= 2:
                return True
        else:
            run = 0
    return False


def recover_filename(cd_name):
    """추출된 Content-Disposition 파일명 값의 인코딩을 복구한다.

    서버가 파일명 바이트를 latin-1로 흘려보내면 모지바케가 온다. 인코딩은
    서버·파일마다 다르다 — 실측:
      - bizinfo: UTF-8 바이트를 latin-1로 디코드 (2026-07-23)
      - SMTECH: 일부 첨부는 **CP949(EUC-KR)** 바이트 + `&amp;` 엔티티 포함
        (2026-07-24) → `Ú 2026³â Áß¼Ò_â¾_ R_amp`처럼 깨졌다.

    규칙 — **모지바케 서명이 있을 때만 재디코드, UTF-8 우선 CP949 폴백**:
      1. cd_name을 latin-1 바이트로 되돌린다. `.encode("latin-1")`이 실패하면
         (이미 정상 한글 등) 재해석하지 않고 원본을 유지한다.
      2. **연속 high-byte 런(_has_mojibake_run)이 있을 때만** 재디코드한다. 이게
         없으면(예: 'AI·DX.pdf'의 고립된 0xB7) 정상 이름이므로 그대로 둔다 —
         cp949가 0xB7을 lead byte로 오인해 'AI텱X.pdf'로 훼손하던 회귀 차단(Codex ir #6).
      3. 런이 있으면 UTF-8 우선(bizinfo류) → 실패 시 CP949(SMTECH류) 폴백.
      4. `html.unescape`를 고정점까지 반복해 잔여 엔티티를 디코드하고,
         %-인코딩은 unquote한다(헤더 파서가 이미 처리했어도 심층 방어)."""
    if not cd_name:
        return cd_name
    try:
        raw = cd_name.encode("latin-1")
    except UnicodeEncodeError:
        raw = None  # 이미 non-latin1(정상 한글 등) — 바이트 재해석 금지
    if raw is not None and _has_mojibake_run(raw):
        try:
            cd_name = raw.decode("utf-8")  # utf-8 성공 → 채택(bizinfo류)
        except UnicodeDecodeError:
            try:
                cd_name = raw.decode("cp949")  # utf-8 실패 시 CP949 폴백(SMTECH류)
            except UnicodeDecodeError:
                pass  # 둘 다 실패 — 원본 유지
    cd_name = _unescape_fixed_point(cd_name)
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
    tmp = None
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
            # 원시 Content-Disposition을 직접 파싱한다 — email의 get_filename()은
            # 따옴표 없는 값의 &amp; 내부 ';'를 파라미터 구분자로 오인해 자른다.
            raw_name = filename_from_content_disposition(
                r.headers.get("Content-Disposition"))
            if raw_name is None:  # Content-Type name= 등 다른 경로는 email 파서 폴백
                raw_name = r.headers.get_filename()
            ctype = (r.headers.get("Content-Type") or "").lower()
            cd_name = recover_filename(raw_name)
            fname = safe_filename(cd_name or fallback_name, idx)
            path = (pathlib.Path(dirpath) / fname).resolve()
            if os.path.commonpath([str(path), dirpath]) != dirpath \
                    or path.is_symlink():
                raise RuntimeError("path_escape_blocked")
            # O_CREAT|O_EXCL|O_NOFOLLOW: 사전 배치된 파일/symlink를 따라가거나
            # 기존 정상 파일을 truncate하지 않는다(Codex ir #5, "wb" 교체). tmp에
            # 받고 성공 시에만 최종 이름으로 교체 — 실패 시 잔여물 없음.
            tmp_path = path.with_name(f".part-{os.getpid()}-{idx}-{path.name}"[:200])
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(tmp_path,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY | nofollow, 0o644)
            except FileExistsError:
                raise RuntimeError(f"tmp_preexists_blocked: {tmp_path.name}") from None
            tmp = tmp_path
            read = 0
            first = b""
            with os.fdopen(fd, "wb") as fh:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    if not first:
                        first = chunk[:512]
                    read += len(chunk)
                    if read > MAX_ATTACH_BYTES:
                        raise RuntimeError(
                            f"첨부가 {MAX_ATTACH_BYTES // (1 << 20)}MB 상한 초과")
                    fh.write(chunk)
            # 200으로 위장한 HTML 오류/CAPTCHA 페이지를 바이너리 첨부로 저장하는 것을
            # 막는다(Codex ir #1) — 첨부 확장자가 문서인데 본문이 HTML이면 실패.
            if _looks_like_html_error(first, ctype, path.name):
                raise RuntimeError("soft_block_html — 첨부가 아닌 HTML 오류/차단 페이지")
            os.replace(tmp, path)
            tmp = None
            return path
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ManualEscalation(f"첨부 다운로드 HTTP {e.code}") from e
        raise
    except (RuntimeError, OSError):
        if tmp is not None:
            tmp.unlink(missing_ok=True)  # 부분 tmp 잔존 방지
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
        if f.get("download_status"):
            # 사전 마킹된 항목(예: 계약 미확정 외부 시스템 링크
            # "skipped_unverified") — 다운로드하지 않고 상태를 보존한다.
            continue
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
            print(f"WARNING [{tag}] attachment {f.get('filename') or '?'}: "
                  f"리다이렉트 차단 — {e}", file=sys.stderr)
            continue
        except (urllib.error.URLError, urllib.error.HTTPError,
                RuntimeError, OSError, TimeoutError) as e:
            f["download_status"] = "failed"
            f["download_reason"] = str(e)
            print(f"WARNING [{tag}] attachment {f.get('filename') or '?'}: {e}",
                  file=sys.stderr)
            continue
        f["local_path"] = str(path)
        f["filename"] = path.name
        f["download_status"] = "ok"
        f["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        attach_hashes.append(f["sha256"])
    return attach_hashes
