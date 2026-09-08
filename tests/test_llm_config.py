from pathlib import Path
import sys

from clawbox.experiments.llm_config import resolve_llm_configuration


def test_existing_clawtune_configuration_supplies_model_and_credential(tmp_path):
    # Use the installed sibling loader, with a small configuration and key file.
    root = Path(__file__).resolve().parents[2] / "ClawTune"
    sys.path.insert(0, str(root))
    try:
        config_dir = tmp_path / "swe_rebench"
        config_dir.mkdir()
        (config_dir / "key.txt").write_text("test-api-key\n")
        path = config_dir / "config.yaml"
        path.write_text("llm:\n  model: actual-model\n  upstream_base_url: http://model.test/v1\n"
                        "  api_key_file: swe_rebench/key.txt\n")
        original = {"clawtune_config": str(path), "openclaw_exec_yield_ms": 120000}
        resolved, credential = resolve_llm_configuration(original, live=True)
        assert resolved["model"] == "actual-model"
        assert resolved["base_url"] == "http://model.test/v1"
        assert credential == "test-api-key"
        assert "api_key" not in resolved
        assert "model" not in original
    finally:
        sys.path.remove(str(root))
