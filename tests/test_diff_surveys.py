"""Contract tests for diff_surveys.py (no network)."""
import json

import pytest


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def ks(sn, title, deadline="2026-07-31"):
    return {"pbancSn": str(sn), "title": title, "start": "2026-07-01",
            "deadline": deadline, "url": f"https://www.k-startup.go.kr/x?pbancSn={sn}"}


def run_diff(diff_surveys, monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["diff_surveys.py", *argv])
    diff_surveys.main()


def test_new_changed_closed_classification(diff_surveys, monkeypatch, tmp_path, capsys):
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [
        ks(1, "그대로인 공고"),
        ks(2, "닫힌 공고"),
        ks(4, "제목이 바뀔 공고"),
    ])
    write_jsonl(curr / "kstartup.jsonl", [
        ks(1, "그대로인 공고"),
        ks(3, "새로 뜬 공고"),
        ks(4, "제목이 바뀐 공고", deadline="2026-08-15"),
    ])
    out = tmp_path / "new_items.jsonl"
    run_diff(diff_surveys, monkeypatch, [str(prev), str(curr), "--out", str(out)])
    text = capsys.readouterr().out
    assert "## NEW (1)" in text and "새로 뜬 공고" in text
    assert "## CHANGED (1)" in text and "title" in text and "apply_end" in text
    assert "## CLOSED (1)" in text and "닫힌 공고" in text
    assert "## UNCHANGED: 1 items" in text
    out_recs = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert {r["pbancSn"] for r in out_recs} == {"3", "4"}  # new + changed only


def test_empty_current_dir_exit1(diff_surveys, monkeypatch, tmp_path):
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [ks(1, "a")])
    curr.mkdir()
    with pytest.raises(SystemExit) as e:
        run_diff(diff_surveys, monkeypatch, [str(prev), str(curr)])
    assert isinstance(e.value.code, str) and e.value.code.startswith("ERROR")


def test_duplicate_key_exit1(diff_surveys, monkeypatch, tmp_path):
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [ks(1, "a")])
    write_jsonl(curr / "a.jsonl", [ks(1, "a")])
    write_jsonl(curr / "b.jsonl", [ks(1, "a")])  # same key in a second file
    with pytest.raises(SystemExit) as e:
        run_diff(diff_surveys, monkeypatch, [str(prev), str(curr)])
    assert "duplicate key" in str(e.value.code)


def test_broken_json_exit1(diff_surveys, monkeypatch, tmp_path):
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [ks(1, "a")])
    curr.mkdir()
    (curr / "kstartup.jsonl").write_text('{"pbancSn": "1", broken\n', encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        run_diff(diff_surveys, monkeypatch, [str(prev), str(curr)])
    assert "broken JSON" in str(e.value.code)


PROFILE_A = """# ir-search 프로필
- 대상: 합성 프로젝트
- 창업 단계: 예비창업자
- 지역 연고: 대구 (이전 가능: 경북)
- 대표자: 30대 / 남
- 필요한 것: 자금, 공간
"""

PROFILE_B = PROFILE_A.replace("예비창업자", "법인 2년차")


def test_profile_fingerprint_mismatch_invalidates_carryover(
        diff_surveys, monkeypatch, tmp_path, capsys):
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [ks(1, "그대로"), ks(2, "그대로2")])
    write_jsonl(curr / "kstartup.jsonl", [ks(1, "그대로"), ks(2, "그대로2")])
    old_p, new_p = tmp_path / "old_profile.md", tmp_path / "new_profile.md"
    old_p.write_text(PROFILE_A, encoding="utf-8")
    new_p.write_text(PROFILE_B, encoding="utf-8")  # 창업 단계 axis changed
    out = tmp_path / "review.jsonl"
    run_diff(diff_surveys, monkeypatch, [
        str(prev), str(curr), "--out", str(out),
        "--old-profile", str(old_p), "--new-profile", str(new_p)])
    text = capsys.readouterr().out
    assert "CARRY-OVER INVALIDATED" in text
    # everything (even UNCHANGED) goes to review
    out_recs = out.read_text(encoding="utf-8").splitlines()
    assert len(out_recs) == 2


def test_profile_one_sided_invalidates(diff_surveys, monkeypatch, tmp_path, capsys):
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [ks(1, "그대로")])
    write_jsonl(curr / "kstartup.jsonl", [ks(1, "그대로")])
    old_p = tmp_path / "old_profile.md"
    old_p.write_text(PROFILE_A, encoding="utf-8")
    run_diff(diff_surveys, monkeypatch,
             [str(prev), str(curr), "--old-profile", str(old_p)])
    assert "CARRY-OVER INVALIDATED" in capsys.readouterr().out


def test_profile_identical_keeps_carryover(diff_surveys, monkeypatch, tmp_path, capsys):
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [ks(1, "그대로")])
    write_jsonl(curr / "kstartup.jsonl", [ks(1, "그대로")])
    old_p, new_p = tmp_path / "old_profile.md", tmp_path / "new_profile.md"
    old_p.write_text(PROFILE_A, encoding="utf-8")
    new_p.write_text(PROFILE_A, encoding="utf-8")
    run_diff(diff_surveys, monkeypatch, [
        str(prev), str(curr), "--old-profile", str(old_p), "--new-profile", str(new_p)])
    text = capsys.readouterr().out
    assert "CARRY-OVER INVALIDATED" not in text
    assert "carry over previous verdicts" in text
