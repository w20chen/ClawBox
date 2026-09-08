import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from trace_fixtures import llm_spans, write_spans


def test_verification_pairs_every_live_session_with_its_original_trace(tmp_path, monkeypatch):
    module_spec = importlib.util.spec_from_file_location(
        "verification", Path(__file__).parents[1] / "scripts/verify-agent-roundtrip.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    base = yaml.safe_load(Path("examples/experiments/getting-started.yaml").read_text())
    config = tmp_path / "task.yaml"
    config.write_text(yaml.safe_dump(base))
    output = tmp_path / "results"
    originals = {}

    def run(command, **kwargs):
        name = command[-1]
        spec = yaml.safe_load(Path(command[-3]).read_text())
        concurrency, = spec["execution"]["concurrency_levels"]
        directory = output / name
        (directory / "arms").mkdir(parents=True)
        (directory / "arms/one.json").write_text(json.dumps({"status": "succeeded"}))
        if spec["inference"]["backend"] == "api":
            for index in range(concurrency):
                path = directory / "runtime-traces" / f"session-{index}" / "native.jsonl"
                path.parent.mkdir(parents=True)
                rows = llm_spans([{"role": "user", "content": str(index)}],
                                 {"content": f"{name}-{index}"})
                write_spans(path, [dict(row, runtime_id=f"session-{index}") for row in rows])
                originals[str(path)] = path.read_bytes()
        else:
            cases = spec["workload"]["cases"]
            assert len(cases) == concurrency
            assert spec["workload"]["session_assignment"] == (
                "round_robin" if concurrency == 4 else "single_case")
            for case in cases:
                path = case["replay_trace_reference"]
                assert Path(path).read_bytes() == originals[path]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.sys, "argv", ["verify", str(config), "--clawtune-config",
                                            str(tmp_path / "clawtune.yaml"), "--output", str(output)])
    module.main()
    results = json.loads((output / "verification.json").read_text())
    assert len(results) == 12
    assert all(row["passed"] for row in results)
    assert len(originals) == 15
