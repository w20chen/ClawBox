"""Tests for scripts/evidence-manifest.py (CBX-M0-005).

The generator must collect a re-verifiable manifest of Git/image/schema/host
facts and per-run artifact digests without reading credential plaintext, and
`verify` must recompute every digest so the same evidence can be re-validated.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "evidence-manifest.py"


def _collect(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "collect",
         "--release", "test-release", "--cluster", "test-cluster", "--run", "test-run-001",
         "--out-root", str(tmp_path), *extra],
        capture_output=True, text=True,
    )


def _manifest_path(tmp_path: Path) -> Path:
    return tmp_path / "test-release" / "test-cluster" / "test-run-001" / "manifest.json"


def test_collect_writes_reverifiable_manifest(tmp_path: Path):
    result = _collect(tmp_path)
    assert result.returncode == 0, result.stderr
    path = _manifest_path(tmp_path)
    assert path.exists()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["release"] == "test-release"
    assert manifest["cluster"] == "test-cluster"
    assert manifest["run_id"] == "test-run-001"
    assert manifest["schema_version"] == 1
    assert manifest["git"]["sha"]
    assert manifest["schemas"]["crd_versions"] == "v1alpha1"
    assert manifest["schemas"]["observation_schema"] == "6"
    assert manifest["schemas"]["runtimeclass"] == "kata-fc-arm64"
    assert manifest["self_hash"]
    assert (path.parent / "gate-summary.json").exists()


def test_collect_records_image_refs_with_digests_or_null(tmp_path: Path):
    result = _collect(tmp_path, "--image", "foo/bar:dev", "--image", "baz@sha256:abcd")
    assert result.returncode == 0, result.stderr
    manifest = json.loads(_manifest_path(tmp_path).read_text(encoding="utf-8"))
    assert manifest["images"] == {"foo/bar:dev": None, "baz@sha256:abcd": None}


def test_gate_summary_records_results(tmp_path: Path):
    result = _collect(tmp_path, "--gate", "e2e-real-task:pass", "--gate", "cleanup:blocked")
    assert result.returncode == 0, result.stderr
    summary = json.loads(
        (tmp_path / "test-release" / "test-cluster" / "test-run-001" / "gate-summary.json")
        .read_text(encoding="utf-8")
    )
    statuses = {g["gate"]: g["status"] for g in summary["gates"]}
    assert statuses == {"e2e-real-task": "pass", "cleanup": "blocked"}


def test_verify_passes_on_unchanged_evidence(tmp_path: Path):
    _collect(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", "--manifest", str(_manifest_path(tmp_path))],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "VERIFY OK" in result.stdout


def test_verify_fails_when_manifest_is_tampered(tmp_path: Path):
    _collect(tmp_path)
    path = _manifest_path(tmp_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["release"] = "tampered"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", "--manifest", str(path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "VERIFY FAILED" in result.stdout
