"""Exercise source selection in a fresh process, without conftest imports."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


def _write_source(source):
    resource = source / "tool_resource"
    timing = source / "tool_time"
    resource.mkdir(parents=True)
    timing.mkdir()
    for package in (resource, timing):
        (package / "__init__.py").write_text("", encoding="utf-8")
    (resource / "runtime_kb.py").write_text(
        "class CompletedCall: pass\nclass RuntimeToolResourceKB: pass\n"
        "class ToolCallQuery: pass\nclass ClauseResourceKB: pass\n",
        encoding="utf-8",
    )
    (resource / "sdk.py").write_text(
        "def _observations_from_call(): pass\ndef _validate_artifact(): pass\n",
        encoding="utf-8",
    )
    (timing / "command.py").write_text(
        "def shell_command_heads(): pass\ndef shell_command_prefix_tokens(): pass\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("adapter", ["clawtune", "native", "scheduler", "scheduler_then_tuning"])
@pytest.mark.parametrize("already_on_path", [False, True])
def test_explicit_clawtune_source_wins_over_sibling_checkout(tmp_path, adapter, already_on_path):
    _check_source_selection(tmp_path, adapter, "explicit", already_on_path)


@pytest.mark.parametrize("adapter", ["clawtune", "native", "scheduler"])
@pytest.mark.parametrize("selection", ["sibling", "installed"])
def test_clawtune_fallback_source(tmp_path, adapter, selection):
    _check_source_selection(tmp_path, adapter, selection)


def test_scheduler_legacy_source_wins_over_sibling_checkout(tmp_path):
    _check_source_selection(tmp_path, "scheduler", "legacy")


def _check_source_selection(tmp_path, adapter, selection, already_on_path=False):
    sources = {
        "explicit": tmp_path / "configured-clawtune",
        "legacy": tmp_path / "legacy-clawtune",
        "sibling": tmp_path / "ClawTune" / "services" / "sidecar" / "src",
        "installed": tmp_path / "installed-clawtune",
    }
    for name, source in sources.items():
        if name != "sibling" or selection != "installed":
            _write_source(source)
    modules = {
        "clawtune": [("clawbox.tuning.clawtune", "_load_clawtune")],
        "native": [("clawbox.tuning.native", "_clawtune_api")],
        "scheduler": [("clawbox.scheduler.kb", "_load_clawtune")],
    }
    modules["scheduler_then_tuning"] = modules["scheduler"] + modules["clawtune"] + modules["native"]
    env = {key: value for key, value in os.environ.items()
           if key not in {"CLAWTUNE_SIDECAR_SRC", "CLAWTUNE_SCHEDULER_SRC"}}
    if selection == "explicit":
        env["CLAWTUNE_SIDECAR_SRC"] = str(sources["explicit"])
        env["CLAWTUNE_SCHEDULER_SRC"] = str(sources["legacy"])
    elif selection == "legacy":
        env["CLAWTUNE_SCHEDULER_SRC"] = str(sources["legacy"])
    else:
        env["CLAWTUNE_SIDECAR_SRC"] = str(tmp_path / "missing-source")
    # Redirect both built-in locations to fixtures; no local ClawTune checkout
    # or /opt installation is needed. Each case starts with a fresh module cache.
    script = textwrap.dedent(f"""
        import importlib
        import sys
        from pathlib import Path
        from unittest.mock import patch

        def source_path(value):
            if value == "/opt/clawtune/services/sidecar/src":
                return Path({str(sources['installed'])!r})
            return Path(value)

        class SourceSearchPath(list):
            def insert(self, index, value):
                super().insert(index, str(source_path(value)))

        sys.path = SourceSearchPath(sys.path)
        if {already_on_path!r}:
            sys.path.insert(0, {str(sources['sibling'])!r})
            sys.path.append({str(sources['explicit'])!r})
        for name, loader in {modules[adapter]!r}:
            module = importlib.import_module(name)
            fake_file = Path({str(tmp_path)!r}) / "ClawBox" / "clawbox" / "adapter" / "loader.py"
            with patch.object(module, "__file__", str(fake_file)), patch.object(module, "Path", source_path):
                getattr(module, loader)()
            import tool_resource.runtime_kb as kb
            assert Path(kb.__file__).resolve() == Path({str(sources[selection] / 'tool_resource' / 'runtime_kb.py')!r}).resolve(), kb.__file__
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
