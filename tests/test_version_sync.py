"""버전 동기화 게이트 — version이 적힌 manifest 4곳이 일치해야 한다.

  root plugin.json / .claude-plugin/plugin.json / .codex-plugin/plugin.json /
  gemini-extension.json  (.gemini-extension 디렉토리는 없고 root의
  gemini-extension.json이 Gemini용 manifest다 — 위치 확인 2026-07-23)

하나라도 어긋나면 릴리스 금지: 최신 버전으로 통일 후 커밋하라.
"""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

VERSION_FILES = [
    "plugin.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "gemini-extension.json",
]

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def read_versions():
    versions = {}
    for rel in VERSION_FILES:
        path = REPO / rel
        assert path.is_file(), f"manifest 누락: {rel}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "version" in data, f"{rel}: version 필드 없음"
        versions[rel] = data["version"]
    return versions


def test_all_manifest_versions_present_and_semver():
    for rel, v in read_versions().items():
        assert SEMVER.match(v), f"{rel}: semver 아님 — {v!r}"


def test_all_manifest_versions_identical():
    versions = read_versions()
    unique = set(versions.values())
    assert len(unique) == 1, (
        "manifest 버전 불일치 — 전부 같은 버전으로 통일하라: "
        + ", ".join(f"{k}={v}" for k, v in sorted(versions.items()))
    )
