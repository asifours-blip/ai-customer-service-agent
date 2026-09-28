"""转换版本封版、摘要校验与固定数据集隔离。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import eval.converted as converted
from eval.loader import load_dataset


@pytest.fixture(autouse=True)
def isolated_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(converted, "CONVERTED_DIR", tmp_path / "converted")


def test_sealed_append_moves_to_next_version_and_preserves_fixed_110() -> None:
    before = load_dataset()
    converted.append_case({"case_id": converted.next_case_id(), "input": "first"})
    manifest = converted.seal_version("v1", "support_agent")
    assert manifest["case_count"] == 1
    assert len(manifest["line_sha256"]) == 1
    assert len(manifest["file_sha256"]) == 64
    assert manifest["actor"] == "support_agent"
    assert manifest["sealed_at"]
    assert converted.current_version() == "v2"
    converted.append_case({"case_id": converted.next_case_id(), "input": "second"})
    assert converted.load_version("v1")[0]["input"] == "first"
    assert converted.load_version("v2")[0]["case_id"] == "fb_v2_001"
    assert load_dataset() == before


def test_tampered_sealed_file_is_rejected_on_evaluation_load() -> None:
    converted.append_case({"case_id": "fb_v1_001", "input": "original"})
    converted.seal_version("v1", "support_agent")
    path = converted.version_path("v1")
    path.write_text(json.dumps({"case_id": "fb_v1_001", "input": "tampered"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sha256 不一致"):
        converted.load_sealed_version("v1")


def test_open_version_is_rejected_on_evaluation_load() -> None:
    converted.append_case({"case_id": "fb_v1_001", "input": "open"})
    with pytest.raises(ValueError, match="尚未封版"):
        converted.load_sealed_version("v1")


def test_sealed_manifest_digest_is_reportable() -> None:
    converted.append_case({"case_id": "fb_v1_001", "input": "sealed"})
    converted.seal_version("v1", "support_agent")
    cases, digest = converted.load_sealed_version("v1")
    assert len(cases) == 1
    assert len(digest) == 64


def test_concurrent_seal_and_append_keep_cases_in_one_valid_version() -> None:
    converted.append_case({"case_id": "fb_v1_001", "input": "first"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        seal = pool.submit(converted.seal_version, "v1", "support_agent")
        appended = pool.submit(converted.append_new_case, {"input": "second"})
        seal.result()
        written_version = appended.result()
    all_cases = converted.load_version("v1") + converted.load_version("v2")
    assert len(all_cases) == 2
    assert len({case["case_id"] for case in all_cases}) == 2
    assert written_version in {"v1", "v2"}
    converted.load_sealed_version("v1")


def test_malformed_manifest_is_rejected() -> None:
    converted.append_case({"case_id": "fb_v1_001", "input": "sealed"})
    converted.seal_version("v1", "support_agent")
    path = converted.manifest_path("v1")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["line_sha256"] = "wrong type"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest 结构不合法"):
        converted.load_sealed_version("v1")


def test_report_records_loaded_version_and_manifest_digest(tmp_path) -> None:
    from eval.report import write_reports

    converted.append_case({"case_id": "fb_v1_001", "input": "sealed"})
    converted.seal_version("v1", "support_agent")
    _, digest = converted.load_sealed_version("v1")
    snapshot = Path(__file__).resolve().parents[2] / "eval" / "reports" / "offline_report.json"
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["dataset"] = {"kind": "converted", "version": "v1", "manifest_sha256": digest}
    paths = write_reports(payload, tmp_path)
    assert json.loads(paths["json"].read_text(encoding="utf-8"))["dataset"]["manifest_sha256"] == digest
    html = paths["html"].read_text(encoding="utf-8")
    assert "转换版本 v1" in html and digest in html
