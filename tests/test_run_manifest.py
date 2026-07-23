"""Schema/atomicity tests for run_manifest.py."""
import json
import os
import re

import pytest


def read_manifest(tmp_path):
    return json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))


def test_make_run_schema_fields(run_manifest):
    run = run_manifest.make_run(
        "kstartup", "ok", 0, pages_fetched=12, collected=178,
        stop_reason="no-new-2pages", errors=[], reported_total=260,
        duplicates=30, cutoff="2026-07-01")
    assert run == {
        "source": "kstartup",
        "status": "ok",
        "exit_code": 0,
        "pages_fetched": 12,
        "collected": 178,
        "stop_reason": "no-new-2pages",
        "cutoff": "2026-07-01",
        "errors": [],
        "reported_total": 260,
        "duplicates": 30,
    }


def test_make_run_optional_fields_omitted(run_manifest):
    run = run_manifest.make_run("nipa", "partial", 2, 3, 40, "error", ["HTTP 500"])
    assert "reported_total" not in run and "duplicates" not in run
    assert run["cutoff"] is None
    assert run["errors"] == ["HTTP 500"]


@pytest.mark.parametrize("status", ["ok", "partial", "manual", "inactive"])
def test_all_status_values_accepted(run_manifest, status):
    assert run_manifest.make_run("x", status, 0, 0, 0, "s")["status"] == status


def test_invalid_status_rejected(run_manifest):
    with pytest.raises(ValueError):
        run_manifest.make_run("x", "success", 0, 0, 0, "s")


def test_update_manifest_creates_file(run_manifest, tmp_path):
    out = tmp_path / "kstartup_all.jsonl"
    run = run_manifest.make_run("kstartup", "ok", 0, 3, 30, "reached-total")
    path = run_manifest.update_manifest(str(out), [run])
    assert path == str(tmp_path / "run_manifest.json")
    m = read_manifest(tmp_path)
    assert m["manifest_schema_version"] == 1
    assert m["runs"] == [run]
    # ISO8601 KST timestamp
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00", m["generated_at"])


def test_update_manifest_appends_and_replaces_same_source(run_manifest, tmp_path):
    out = tmp_path / "x.jsonl"
    run_manifest.update_manifest(str(out), [
        run_manifest.make_run("kstartup", "partial", 2, 1, 10, "network-error", ["boom"])])
    run_manifest.update_manifest(str(out), [
        run_manifest.make_run("bizinfo", "ok", 0, 5, 75, "no-new-2pages")])
    # re-run kstartup: its old entry must be REPLACED, bizinfo kept
    run_manifest.update_manifest(str(out), [
        run_manifest.make_run("kstartup", "ok", 0, 12, 180, "reached-total")])
    m = read_manifest(tmp_path)
    assert len(m["runs"]) == 2
    by_source = {r["source"]: r for r in m["runs"]}
    assert by_source["kstartup"]["status"] == "ok"
    assert by_source["kstartup"]["collected"] == 180
    assert by_source["bizinfo"]["collected"] == 75


def test_update_manifest_atomic_replace_and_no_tmp_leftovers(run_manifest, tmp_path, monkeypatch):
    calls = []
    real_replace = os.replace

    def spy_replace(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(run_manifest.os, "replace", spy_replace)
    out = tmp_path / "x.jsonl"
    run_manifest.update_manifest(str(out), [run_manifest.make_run("a", "ok", 0, 1, 1, "s")])
    run_manifest.update_manifest(str(out), [run_manifest.make_run("b", "ok", 0, 1, 1, "s")])
    assert len(calls) == 2  # written via tmp → os.replace every time
    for src, dst in calls:
        assert src != dst and dst.endswith("run_manifest.json")
    leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    assert leftovers == []
    read_manifest(tmp_path)  # still valid JSON


def test_update_manifest_preserves_corrupt_manifest(run_manifest, tmp_path, capsys):
    """Corrupt manifest data must NEVER be silently erased: the broken file is
    renamed to run_manifest.json.corrupt-<ts>, the new manifest records the
    recovery, and a WARNING goes to stderr."""
    (tmp_path / "run_manifest.json").write_text("{ not json", encoding="utf-8")
    out = tmp_path / "x.jsonl"
    run_manifest.update_manifest(str(out), [run_manifest.make_run("a", "ok", 0, 1, 1, "s")])
    m = read_manifest(tmp_path)
    assert [r["source"] for r in m["runs"]] == ["a"]
    # the corrupt bytes are preserved verbatim under a timestamped name...
    corrupt_name = m["recovered_from_corrupt"]
    assert re.fullmatch(r"run_manifest\.json\.corrupt-\d{8}-\d{6}(\.\d+)?", corrupt_name)
    assert (tmp_path / corrupt_name).read_text(encoding="utf-8") == "{ not json"
    # ...and the operator is warned on stderr
    assert "WARNING" in capsys.readouterr().err


def test_update_manifest_preserves_schema_mismatched_manifest(run_manifest, tmp_path):
    """A top-level list (or any non-v1 shape) counts as corrupt: preserve it."""
    (tmp_path / "run_manifest.json").write_text('[1, 2, 3]', encoding="utf-8")
    out = tmp_path / "x.jsonl"
    run_manifest.update_manifest(str(out), [run_manifest.make_run("a", "ok", 0, 1, 1, "s")])
    m = read_manifest(tmp_path)
    assert [r["source"] for r in m["runs"]] == ["a"]
    corrupt_name = m["recovered_from_corrupt"]
    assert (tmp_path / corrupt_name).read_text(encoding="utf-8") == "[1, 2, 3]"


def test_update_manifest_valid_manifest_not_flagged_as_recovered(run_manifest, tmp_path):
    out = tmp_path / "x.jsonl"
    run_manifest.update_manifest(str(out), [run_manifest.make_run("a", "ok", 0, 1, 1, "s")])
    run_manifest.update_manifest(str(out), [run_manifest.make_run("b", "ok", 0, 1, 1, "s")])
    m = read_manifest(tmp_path)
    assert "recovered_from_corrupt" not in m
    assert not [p for p in os.listdir(tmp_path) if ".corrupt-" in p]
