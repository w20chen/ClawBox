"""Static contracts for the target-host P0 orchestration helpers."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_submit_exports_requested_deadline_to_payload_builder() -> None:
    script = (ROOT / "scripts" / "m1-kb-submit.sh").read_text(encoding="utf-8")

    assert 'DEADLINE="${1:-600}"\n  export DEADLINE' in script
    assert 'os.environ.get(\'DEADLINE\', \'600\')' in script
    assert 'flock -n 9' in script
    assert 'MISSING >= 4' in script


def test_joincheck_streams_pod_traces_to_host_and_fails_closed() -> None:
    script = (ROOT / "scripts" / "m1-p0-joincheck.sh").read_text(encoding="utf-8")

    assert 'exec -i "$IPOD"' in script
    assert 'SELECT relative_path, offset, payload_base64, final FROM trace_chunks' in script
    assert '> "$OUT/traces.tar.gz"' in script
    assert 'tar -xzf "$OUT/traces.tar.gz" -C "$OUT"' in script
    assert '--field-selector=status.phase=Running' in script
    assert "sources.get('runtime-envelope', 0) == 0" in script
    assert "joined spans without runtime-envelope provenance" in script
    assert "if not llm_spans" in script
    assert "if span_ids != matched" in script
    assert "patch len:[[:space:]]*[1-9][0-9]*" in script
