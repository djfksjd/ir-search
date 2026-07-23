#!/usr/bin/env bash
# ir-search one-command installer
#   curl -fsSL https://raw.githubusercontent.com/djfksjd/ir-search/main/install.sh | bash
#
# Detects installed host CLIs (claude / codex / agy / gemini) and installs the
# plugin/extension for each. If no CLI is found, falls back to cloning into
# ~/.agents/skills/ir-search (picked up by Cursor, Grok Build, and other
# file-based hosts). Per-host failures are non-fatal.
set -u

REPO="djfksjd/ir-search"
REPO_URL="https://github.com/${REPO}.git"
INSTALLED=0

log()  { printf '\033[1;32m[ir-search]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[ir-search]\033[0m %s\n' "$*" >&2; }

try_host() { # <name> <command...>
  local name="$1"; shift
  if "$@"; then
    log "✓ ${name} 설치 완료"
    INSTALLED=$((INSTALLED + 1))
  else
    warn "✗ ${name} 설치 실패 — 수동 설치는 README 참조"
  fi
}

if command -v claude >/dev/null 2>&1; then
  try_host "Claude Code" bash -c \
    "claude plugin marketplace add ${REPO} && claude plugin install ir-search@djfksjd"
fi

if command -v codex >/dev/null 2>&1; then
  try_host "Codex" bash -c \
    "codex plugin marketplace add ${REPO} && codex plugin add ir-search@djfksjd"
fi

if command -v agy >/dev/null 2>&1; then
  try_host "agy (Antigravity CLI)" bash -c \
    "agy plugin install ${REPO} && agy plugin enable ir-search"
fi

if command -v gemini >/dev/null 2>&1; then
  try_host "Gemini CLI" gemini extensions install "https://github.com/${REPO}"
fi

# File-based hosts (Cursor, Grok Build, ...) read ~/.agents/skills/.
# Also serves as the fallback when no host CLI was detected.
SKILL_DIR="${HOME}/.agents/skills/ir-search"
if [ -d "${SKILL_DIR}/.git" ]; then
  if git -C "${SKILL_DIR}" pull --ff-only >/dev/null 2>&1; then
    log "✓ ~/.agents/skills/ir-search 갱신 (Cursor·Grok Build 등)"
    INSTALLED=$((INSTALLED + 1))  # an update is a successful install too
  else
    warn "✗ ~/.agents/skills/ir-search 갱신 실패 — 수동으로 git pull 하세요"
  fi
elif [ ! -e "${SKILL_DIR}" ]; then
  mkdir -p "${HOME}/.agents/skills"
  if git clone --quiet "${REPO_URL}" "${SKILL_DIR}"; then
    log "✓ ~/.agents/skills/ir-search clone (Cursor·Grok Build 등)"
    INSTALLED=$((INSTALLED + 1))
  else
    warn "✗ clone 실패: ${SKILL_DIR}"
  fi
fi

# Dependency: curl_cffi (TLS-fingerprint evasion for the crawlers)
if python3 -c 'import curl_cffi' >/dev/null 2>&1; then
  log "✓ curl_cffi 이미 설치됨"
elif pip3 install 'curl_cffi>=0.15' >/dev/null 2>&1 \
  || pip3 install --user 'curl_cffi>=0.15' >/dev/null 2>&1; then
  log "✓ curl_cffi 설치 완료"
else
  warn "✗ curl_cffi 설치 실패 — 크롤러 첫 실행 전 수동 설치: pip3 install 'curl_cffi>=0.15'"
  warn "  (PEP 668 'externally-managed-environment' 오류라면: pip3 install --break-system-packages 'curl_cffi>=0.15' 또는 pipx/venv 사용)"
fi

if [ "${INSTALLED}" -eq 0 ]; then
  warn "설치된 호스트가 없습니다. 지원 호스트: Claude Code·Codex·agy·Gemini CLI·Cursor·Grok Build (README 참조)"
  exit 1
fi
log "완료 — 새 세션에서 '지원사업 전수조사 해줘'로 사용하세요."
