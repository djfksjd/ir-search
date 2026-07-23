"""공통 diff 레코드 JSON 스키마 conformance 테스트.

references/diff_record_schema.json이 ir/sole diff 출력 공통 계약의 단일
기준이다. jsonschema 의존성을 강제하지 않기 위해 스키마가 사용하는 키워드
부분집합(type/enum/required/properties/items/minLength)만 지원하는 미니
검증기를 내장한다 — 스키마에 이 밖의 키워드를 추가하면 테스트가 실패한다
(모르는 키워드는 무시가 아니라 에러).
"""
import json
from pathlib import Path

import pytest

SCHEMA_PATH = (Path(__file__).resolve().parent.parent / "skills" / "ir-search"
               / "references" / "diff_record_schema.json")
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "diff_conformance.jsonl"

SUPPORTED_KEYWORDS = {"type", "enum", "required", "properties", "items",
                      "minLength", "description", "title",
                      "$schema", "$id", "additionalProperties"}

TYPES = {"object": dict, "array": list, "string": str, "integer": int,
         "boolean": bool, "null": type(None), "number": (int, float)}


def validate(instance, schema, path="$"):
    """지원 키워드만으로 검증. 위반은 (path, reason) 목록으로 반환."""
    errors = []
    unknown = set(schema) - SUPPORTED_KEYWORDS
    if unknown:
        raise AssertionError(f"{path}: 미지원 스키마 키워드 {unknown} — "
                             "미니 검증기를 확장하거나 스키마를 단순화하라")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append((path, f"enum 위반: {instance!r}"))
    if "type" in schema:
        types = schema["type"]
        if isinstance(types, str):
            types = [types]
        ok = any(isinstance(instance, TYPES[t])
                 and not (t in ("integer", "number") and isinstance(instance, bool))
                 for t in types)
        if not ok:
            errors.append((path, f"type 위반: {types} != {type(instance).__name__}"))
            return errors
    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errors.append((path, f"required 누락: {req}"))
        for key, sub in schema.get("properties", {}).items():
            if key in instance:
                errors.extend(validate(instance[key], sub, f"{path}.{key}"))
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    if isinstance(instance, str) and "minLength" in schema \
            and len(instance) < schema["minLength"]:
        errors.append((path, f"minLength {schema['minLength']} 위반"))
    return errors


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fixture_lines():
    lines = [json.loads(ln) for ln in
             FIXTURE_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "conformance fixture가 비어 있다"
    return lines


def test_schema_file_is_valid_json_with_contract_fields(schema):
    assert schema["required"] == ["kind", "diff_status", "changed_fields", "record"]
    assert set(schema["properties"]["kind"]["enum"]) == {
        "NEW", "CHANGED", "NEEDS_REHASH", "GONE", "UNCHANGED"}
    rec = schema["properties"]["record"]
    assert rec["required"] == ["source", "source_id", "title"]
    for field in ("content_hash", "hash_version", "attachments",
                  "attachments_complete", "apply_start", "apply_end", "agency"):
        assert field in rec["properties"], f"record.{field} 정의 누락"


def test_conformance_fixture_validates(schema, fixture_lines):
    for i, rec in enumerate(fixture_lines):
        errs = validate(rec, schema)
        assert not errs, f"fixture line {i + 1}: {errs}"


def test_fixture_covers_all_kinds_and_both_skill_styles(fixture_lines):
    kinds = {r["kind"] for r in fixture_lines}
    assert kinds == {"NEW", "CHANGED", "NEEDS_REHASH", "GONE", "UNCHANGED"}
    sources = {r["record"]["source"] for r in fixture_lines}
    assert {"kstartup", "bizinfo"} <= sources          # ir-search 스타일
    assert "sbiz24" in sources                          # sole-search 스타일
    statuses = {a.get("download_status")
                for r in fixture_lines for a in r["record"].get("attachments", [])}
    assert {"ok", "skipped_robots"} <= statuses


def test_kind_equals_diff_status(fixture_lines):
    """스키마로 표현 불가한 계약 — kind와 diff_status는 항상 동일."""
    for r in fixture_lines:
        assert r["kind"] == r["diff_status"]


@pytest.mark.parametrize("mutate, reason", [
    (lambda r: r.pop("kind"), "kind 누락"),
    (lambda r: r.update(kind="WEIRD"), "kind enum 위반"),
    (lambda r: r["record"].pop("source_id"), "record.source_id 누락"),
    (lambda r: r["record"].update(source_id=""), "source_id minLength"),
    (lambda r: r["record"].update(hash_version="3"), "hash_version 타입"),
    (lambda r: r["record"]["attachments"].append({"filename": "no-url.pdf"}),
     "attachment.url 누락"),
    (lambda r: r["record"]["attachments"][0].update(download_status="downloaded"),
     "download_status enum 위반"),
])
def test_invalid_records_rejected(schema, fixture_lines, mutate, reason):
    rec = json.loads(json.dumps(fixture_lines[1]))  # attachments가 있는 CHANGED 건
    mutate(rec)
    assert validate(rec, schema), f"위반이 통과됨: {reason}"
