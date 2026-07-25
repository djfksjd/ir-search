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
    # no manifest here → assert-complete signals these were full crawls so GONE
    # is authorized (coverage-honesty default otherwise suppresses removals)
    run_diff(diff_surveys, monkeypatch,
             [str(prev), str(curr), "--out", str(out), "--assume-complete"])
    text = capsys.readouterr().out
    assert "## NEW (1)" in text and "새로 뜬 공고" in text
    assert "## CHANGED (1)" in text and "title" in text and "apply_end" in text
    assert "## CLOSED (1)" in text and "닫힌 공고" in text
    assert "## UNCHANGED: 1 items" in text
    # --out은 공통 diff 레코드 wrapper(kind/diff_status/changed_fields/record)
    out_recs = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert {r["record"]["pbancSn"] for r in out_recs} == {"3", "4"}  # new + changed
    assert {r["kind"] for r in out_recs} == {"NEW", "CHANGED"}
    for r in out_recs:
        assert r["diff_status"] == r["kind"]
        assert r["record"]["source"] == "kstartup"
        assert r["record"]["source_id"] == r["record"]["pbancSn"]
    # GONE(닫힌 공고)은 별도 gone_ 파일로
    gone = [json.loads(x) for x in
            (out.parent / f"gone_{out.name}").read_text(encoding="utf-8").splitlines()]
    assert [g["kind"] for g in gone] == ["GONE"]
    assert gone[0]["record"]["title"] == "닫힌 공고"


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


# ---- content_hash / hash_version 계약 (Codex 게이트 #2 회귀) ----------------

def h(rec, content_hash, version=2):
    rec = dict(rec)
    rec["content_hash"] = content_hash
    rec["hash_version"] = version
    return rec


def test_classify_body_change_detected_via_content_hash(diff_surveys):
    old = h(ks(1, "같은 제목"), "aaa", 2)
    new = h(ks(1, "같은 제목"), "bbb", 2)
    r = diff_surveys.classify(old, new)
    assert r["kind"] == "CHANGED"
    assert r["changed_fields"] == ["content_hash"]


def test_classify_hash_version_switch_is_one_time_changed(diff_surveys):
    """v2↔v3 산식 전환은 값 비교 불가 — NEEDS_REHASH 영구 루프 대신 1회 CHANGED."""
    old = h(ks(1, "같은 제목"), "aaa", 2)
    new = h(ks(1, "같은 제목"), "bbb", 3)
    r = diff_surveys.classify(old, new)
    assert r["kind"] == "CHANGED"
    assert r["changed_fields"] == ["hash_version(산식 전환 — 1회 상세 재검증)"]


def test_classify_vanished_hash_needs_rehash(diff_surveys):
    old = h(ks(1, "같은 제목"), "aaa", 2)
    new = ks(1, "같은 제목")  # 새 조사에 해시 없음
    assert diff_surveys.classify(old, new)["kind"] == "NEEDS_REHASH"


def test_classify_same_hash_unchanged(diff_surveys):
    old = h(ks(1, "같은 제목"), "aaa", 2)
    assert diff_surveys.classify(old, dict(old))["kind"] == "UNCHANGED"


def test_diff_end_to_end_hash_change_not_carried_over(
        diff_surveys, monkeypatch, tmp_path, capsys):
    """본문(해시)만 바뀐 공고가 UNCHANGED로 승계되지 않는다 (SKILL 계약)."""
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [h(ks(1, "동일 목록 필드"), "aaa", 2)])
    write_jsonl(curr / "kstartup.jsonl", [h(ks(1, "동일 목록 필드"), "bbb", 2)])
    out = tmp_path / "new_items.jsonl"
    run_diff(diff_surveys, monkeypatch, [str(prev), str(curr), "--out", str(out)])
    text = capsys.readouterr().out
    assert "## CHANGED (1)" in text and "## UNCHANGED: 0 items" in text
    (rec,) = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert rec["kind"] == "CHANGED"
    assert rec["changed_fields"] == ["content_hash"]


def test_diff_end_to_end_needs_rehash_emitted_to_out(
        diff_surveys, monkeypatch, tmp_path, capsys):
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [h(ks(1, "동일"), "aaa", 2)])
    write_jsonl(curr / "kstartup.jsonl", [ks(1, "동일")])
    out = tmp_path / "new_items.jsonl"
    run_diff(diff_surveys, monkeypatch, [str(prev), str(curr), "--out", str(out)])
    assert "## NEEDS_REHASH (1)" in capsys.readouterr().out
    (rec,) = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert rec["kind"] == "NEEDS_REHASH"


def test_gone_file_not_loaded_as_raw_crawl(diff_surveys, monkeypatch, tmp_path):
    """이전 diff가 남긴 gone_*.jsonl이 다음 diff에서 원시 수집으로 오인되지 않는다."""
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [ks(1, "a")])
    write_jsonl(curr / "kstartup.jsonl", [ks(1, "a")])
    (curr / "gone_new_items.jsonl").write_text(
        json.dumps({"kind": "GONE", "diff_status": "GONE", "changed_fields": [],
                    "record": ks(99, "지난 소멸")}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    out = tmp_path / "out.jsonl"
    run_diff(diff_surveys, monkeypatch, [str(prev), str(curr), "--out", str(out)])
    assert out.read_text(encoding="utf-8") == ""  # 소멸 잔재가 NEW로 안 뜬다


def test_partial_current_run_suppresses_closed(diff_surveys, monkeypatch, tmp_path,
                                               capsys):
    """현재 run_manifest.json이 kstartup을 partial(api-window)로 표기하면, 이전에
    있던 공고의 부재를 CLOSED로 결론짓지 않는다 — partial 수집을 '전부 소멸'로
    오인하지 않는 커버리지 정직성 계약(Codex #2)."""
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [ks(101, "a"), ks(102, "b")])
    write_jsonl(curr / "kstartup.jsonl", [ks(101, "a")])  # 102 부재
    (curr / "run_manifest.json").write_text(json.dumps({
        "manifest_schema_version": 1, "generated_at": "2026-07-25T00:00:00+09:00",
        "runs": [{"source": "kstartup", "status": "partial", "exit_code": 2,
                  "pages_fetched": 1, "collected": 1, "stop_reason": "api-window"}],
    }), encoding="utf-8")
    out = tmp_path / "out.jsonl"
    run_diff(diff_surveys, monkeypatch, [str(prev), str(curr), "--out", str(out)])
    o = capsys.readouterr().out
    assert "## CLOSED (0)" in o
    assert "CLOSED SUPPRESSED" in o
    assert (tmp_path / "gone_out.jsonl").read_text(encoding="utf-8") == ""


def test_source_absent_from_manifest_suppresses_closed(diff_surveys, monkeypatch,
                                                       tmp_path, capsys):
    """현재 매니페스트에 kstartup 실행 기록이 아예 없으면(다른 소스만 ok) kstartup
    커버리지가 증명되지 않았으므로 kstartup 공고 부재를 CLOSED로 결론짓지
    않는다 — GONE은 '전수 증명된 소스'에만 허용(Codex #2 residual)."""
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [ks(101, "a"), ks(102, "b")])
    write_jsonl(curr / "kstartup.jsonl", [ks(101, "a")])  # 102 부재
    (curr / "run_manifest.json").write_text(json.dumps({
        "manifest_schema_version": 1, "generated_at": "2026-07-25T00:00:00+09:00",
        "runs": [{"source": "bizinfo", "status": "ok", "exit_code": 0,
                  "pages_fetched": 1, "collected": 1, "stop_reason": "done"}],
    }), encoding="utf-8")  # kstartup 항목 없음
    out = tmp_path / "out.jsonl"
    run_diff(diff_surveys, monkeypatch, [str(prev), str(curr), "--out", str(out)])
    o = capsys.readouterr().out
    assert "## CLOSED (0)" in o
    assert "CLOSED SUPPRESSED" in o
    assert (tmp_path / "gone_out.jsonl").read_text(encoding="utf-8") == ""


def test_ok_current_run_reports_closed(diff_surveys, monkeypatch, tmp_path, capsys):
    """반대로 현재 run이 ok(전수 증명)면 102 부재는 정상적으로 CLOSED."""
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [ks(101, "a"), ks(102, "b")])
    write_jsonl(curr / "kstartup.jsonl", [ks(101, "a")])
    (curr / "run_manifest.json").write_text(json.dumps({
        "manifest_schema_version": 1, "generated_at": "2026-07-25T00:00:00+09:00",
        "runs": [{"source": "kstartup", "status": "ok", "exit_code": 0,
                  "pages_fetched": 3, "collected": 1, "stop_reason": "api"}],
    }), encoding="utf-8")
    out = tmp_path / "out.jsonl"
    run_diff(diff_surveys, monkeypatch, [str(prev), str(curr), "--out", str(out)])
    o = capsys.readouterr().out
    assert "## CLOSED (1)" in o
    gone = [x for x in (tmp_path / "gone_out.jsonl").read_text().splitlines() if x.strip()]
    assert len(gone) == 1


def test_first_hash_appearance_is_changed(diff_surveys, monkeypatch, tmp_path, capsys):
    """직전엔 해시가 없다가 이번에 상세를 처음 수집 → CHANGED 재검토(Codex #6)."""
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [ks(1, "공고")])  # 해시 없음
    rec = ks(1, "공고"); rec["content_hash"] = "h1"; rec["hash_version"] = "v3"
    write_jsonl(curr / "kstartup.jsonl", [rec])
    out = tmp_path / "out.jsonl"
    run_diff(diff_surveys, monkeypatch,
             [str(prev), str(curr), "--out", str(out), "--assume-complete"])
    assert "## CHANGED (1)" in capsys.readouterr().out
    (r,) = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert r["kind"] == "CHANGED" and any("최초 상세수집" in c for c in r["changed_fields"])


def test_duplicate_json_keys_rejected(diff_surveys, monkeypatch, tmp_path):
    """한 줄에 중복 JSON 키(위조 필드)면 즉시 실패(Codex #12)."""
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [ks(1, "a")])
    (curr / "kstartup.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (curr / "kstartup.jsonl").write_text(
        '{"pbancSn":"1","pbancSn":"9","title":"x","deadline":"2026-08-01"}\n',
        encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        run_diff(diff_surveys, monkeypatch, [str(prev), str(curr), "--out",
                                             str(tmp_path / "o.jsonl")])
    assert e.value.code != 0


def test_incomplete_profile_invalidates_carryover(diff_surveys, monkeypatch,
                                                  tmp_path, capsys):
    """판정 축이 하나라도 빠진 프로필은 승계 무효 — 전건 재검토(Codex #14)."""
    prev, curr = tmp_path / "prev", tmp_path / "curr"
    write_jsonl(prev / "kstartup.jsonl", [ks(1, "a")])
    write_jsonl(curr / "kstartup.jsonl", [ks(1, "a")])
    full = tmp_path / "full.md"
    full.write_text("- 창업 단계: 예비창업\n- 지역 연고: 충남\n- 대표자: 만 39세 이하\n"
                    "- 필요한 것: 자금\n", encoding="utf-8")
    partial = tmp_path / "partial.md"  # 대표자 축 누락
    partial.write_text("- 창업 단계: 예비창업\n- 지역 연고: 충남\n- 필요한 것: 자금\n",
                       encoding="utf-8")
    run_diff(diff_surveys, monkeypatch,
             [str(prev), str(curr), "--out", str(tmp_path / "o.jsonl"),
              "--old-profile", str(full), "--new-profile", str(partial),
              "--assume-complete"])
    assert "CARRY-OVER INVALIDATED" in capsys.readouterr().out
