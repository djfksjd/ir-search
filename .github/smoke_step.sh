#!/usr/bin/env bash
# 스모크 스텝 래퍼 — 네트워크 도달 실패와 사이트 계약 파손을 구분한다.
#
# 이 canary의 목적은 "소스 사이트의 API/HTML 계약이 조용히 깨진 것"을 잡는 것이다.
# 그런데 크롤러의 exit 2는 계약 파손(첫 페이지 0건 파싱)과 단순 네트워크 도달
# 실패(타임아웃/연결 거부)를 구분하지 않는다. GitHub 러너는 해외 IP라서
# 일부 한국 공공기관 사이트에 도달하지 못하는데(2026-07-27 진단: seoulshinbo는
# urllib·curl_cffi 두 백엔드 모두 TCP connect 30s 타임아웃, 같은 시각 한국 IP
# 로컬에서는 정상 수집), 그걸 계약 파손으로 보고하면 canary는 매번 빨간불이 되고
# 진짜 파손이 묻힌다.
#
# 규칙:
#   - 실패했고 출력에 네트워크 도달 실패 신호가 있으면 → ::warning:: 후 통과(soft)
#   - 그 외의 실패는 그대로 job 실패(hard) — 계약 파손이 곧 알림이다
# 네트워크 신호를 먼저 판정하는 게 맞다. 도달 실패는 "0건 파싱"을 부수적으로
# 유발하지만, 반대로 네트워크가 멀쩡한데 파싱만 0건이면 타임아웃 문구가 없다.
#
# 일시적 flake(2026-07-27 관측: bizinfo가 한 실행에서 타임아웃, 3분 뒤 정상)는
# 30초 뒤 1회 재시도로 흡수한다.
#
# 사용법:  smoke_step.sh <라벨> <명령> [인자...]
# 환경변수: SMOKE_SKIP_RC — 이 종료 코드는 정상 skip으로 취급(예: gov24의 4=키 미등록)
set -uo pipefail

LABEL="$1"
shift

# 네트워크/전송 계층 도달 실패 신호. 계약 파손과 달리 대상 사이트의 응답 내용을
# 한 번도 못 본 경우다. curl 28 = CURLE_OPERATION_TIMEDOUT, 7 = COULDNT_CONNECT.
NET_PATTERNS='timed out|timeout|HTTP 28|curl: \(28\)|curl: \(7\)|urlopen error|Connection refused|Connection reset|Temporary failure in name resolution|Remote end closed|EOF occurred'

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

run_once() {
  set -o pipefail
  "$@" 2>&1 | tee "$OUT"
  return "${PIPESTATUS[0]}"
}

run_once "$@"
rc=$?

if [ "$rc" -ne 0 ] && [ "${SMOKE_SKIP_RC:-}" = "$rc" ]; then
  echo "[$LABEL] exit $rc — 정상 skip 계약, 통과 처리"
  exit 0
fi

if [ "$rc" -ne 0 ] && grep -qiE "$NET_PATTERNS" "$OUT"; then
  echo "[$LABEL] 네트워크 도달 실패로 보임 — 30초 후 1회 재시도"
  sleep 30
  run_once "$@"
  rc=$?
fi

if [ "$rc" -eq 0 ]; then
  exit 0
fi

if [ "${SMOKE_SKIP_RC:-}" = "$rc" ]; then
  echo "[$LABEL] exit $rc — 정상 skip 계약, 통과 처리"
  exit 0
fi

if grep -qiE "$NET_PATTERNS" "$OUT"; then
  echo "::warning title=smoke unreachable::[$LABEL] 재시도 후에도 네트워크 도달 실패 (exit $rc). 러너(해외 IP)에서 대상 사이트에 접속하지 못한 것으로, 사이트 계약 파손이 아니다. 한국 IP에서 크롤러를 직접 돌려 확인할 것."
  {
    echo "### ⚠️ $LABEL — 도달 실패 (계약 파손 아님)"
    echo ""
    echo '```'
    tail -20 "$OUT"
    echo '```'
  } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
  exit 0
fi

echo "::error title=smoke contract broken::[$LABEL] 계약 파손 의심 (exit $rc) — 파싱 결과/스키마를 확인할 것"
exit "$rc"
