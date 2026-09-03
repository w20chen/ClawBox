from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_installer_preserves_helm_database_templates() -> None:
    script = (ROOT / "scripts" / "install-cubesandbox-kunpeng920.sh").read_text(
        encoding="utf-8"
    )

    assert 'include "cube.dbHost"' in script
    assert 'include "cube.redisNodes"' in script
    assert "target.write_text(source.read_text())" not in script
    assert "overcommit_ratio" in script
    assert "rewrite stop name regex (.*)[.]cube[.]local[.]?" in script
    assert "answer auto" in script


def test_template_helper_exposes_cube_command_ports() -> None:
    helper = (ROOT / "scripts" / "register-cube-template.py").read_text(
        encoding="utf-8"
    )

    assert "exposed_ports=[49999, 49983]" in helper
    assert "probe_port=49999" in helper
