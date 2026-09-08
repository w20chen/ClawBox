import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def test_header_preparation_uses_running_guest_config_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "cubesandbox", SimpleNamespace(
        Sandbox=SimpleNamespace(create=None), NEVER_TIMEOUT=-1))
    spec = importlib.util.spec_from_file_location(
        "headers", Path(__file__).parents[1] / "scripts/prepare-guest-kernel-headers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = "# Running kernel\nCONFIG_ARM64=y\n# CONFIG_VIRT_CPU_ACCOUNTING_GEN is not set\n"
    killed = []

    class Guest:
        def __init__(self):
            self.commands = self

        def run(self, command, **kwargs):
            return SimpleNamespace(exit_code=0, stderr="", stdout=(
                "6.18.28\n" + config if command.startswith("uname") else "old configuration"))

        def kill(self):
            killed.append(True)

    monkeypatch.setattr(module.Sandbox, "create", lambda **kwargs: Guest())
    output = tmp_path / "headers"

    def build(command, **kwargs):
        assert killed == [True]
        assert command[:2] == ["docker", "build"]
        assert (output / "running-kernel.config").read_text() == config
        dockerfile = (output / "Dockerfile").read_text()
        assert "/lib/modules/6.18.28/build/.config" in dockerfile
        assert "olddefconfig prepare modules_prepare" in dockerfile

    monkeypatch.setattr(module.subprocess, "run", build)
    monkeypatch.setattr("sys.argv", ["prepare", "--template", "tool", "--node", "node",
                                    "--image", "old", "--tag", "new", "--output", str(output)])
    module.main()
    assert (output / "result.json").is_file()
