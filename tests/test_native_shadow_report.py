from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def load_report_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "native-shadow-report.py"
    spec = importlib.util.spec_from_file_location("native_shadow_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shadow_report_joins_prediction_actual_and_run_a_evidence(tmp_path, monkeypatch):
    module = load_report_module()
    resource = tmp_path / "tool-resource"
    resource.mkdir()
    start = {
        "schema_version": 6,
        "record_type": "span_start",
        "run_id": "run-b",
        "execution": {"execution_id": "exec-b"},
        "prediction": {"tool_resource": {
            "repo": "github.com/acme/foo",
            "command": "python -m pytest -q",
            "prediction": {
                "bucket_id": 3,
                "scope": "repo",
                "key_kind": "exact_clause",
                "evidence_count": 1,
                "fallback_path": ["repo:exact_clause"],
            },
            "clause_predictions": [],
            "continuous_predictions": {
                "peak_cpu_cores": {"conditional_p90": 1.25, "evidence_count": 1}
            },
        }},
    }
    (tmp_path / "run.jsonl").write_text(json.dumps(start) + "\n", encoding="utf-8")
    (resource / "cgroup-resource-exec-b.json").write_text(json.dumps({
        "schema": "cgroup_resource_v1",
        "execution_id": "exec-b",
        "source": "cgroup-v2",
        "sampling_quality": "valid",
        "ts_start": 100.0,
        "ts_end": 102.5,
        "cpu_utilization_avg_cores": 1.1,
        "memory_rss_peak_bytes": 8 * 1024**2,
    }), encoding="utf-8")
    metadata = {
        "tenant_id": "tenant-a",
        "repo_fingerprint": "github.com/acme/foo",
        "generation": 1,
        "pair_digest": "a" * 64,
        "source_digest": "b" * 64,
        "clawtune_revision": "e91e60bc1e5f3209fbcf6091013fde96f217e2a7",
        "evidence": {"runs": ["run-a"]},
    }
    monkeypatch.setenv("CLAWBOX_RUN_ID", "run-b")
    monkeypatch.setenv("CLAWBOX_ATTEMPT_ID", "attempt-b")
    monkeypatch.setenv("CLAWBOX_RESOURCE_PROFILE", "small")
    report = module.build_report(tmp_path, metadata)
    row = report["predictions"][0]
    assert row["kb_generation"] == 1
    assert row["evidence_runs"] == ["run-a"]
    assert row["match_level"] == "repo:exact_clause"
    assert row["evidence_count"] == 1
    assert row["actual_values"]["duration_ms"] == 2500.0
    assert report["resource_control"] == {
        "authoritative_sizer": "FixedProfileSizer",
        "profile": "small",
        "prediction_mode": "shadow",
        "prediction_controls_resources": False,
    }
