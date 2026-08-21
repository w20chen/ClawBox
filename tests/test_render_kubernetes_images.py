from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_renderer():
    path = ROOT / "scripts" / "render-kubernetes-images.py"
    spec = importlib.util.spec_from_file_location("render_kubernetes_images", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_render_replaces_all_platform_tags_with_digests(tmp_path):
    renderer = load_renderer()
    images = {
        "control": f"registry.example/clawbox/control@sha256:{'1' * 64}",
        "runtime": f"registry.example/clawbox/runtime@sha256:{'2' * 64}",
        "tool_bridge": f"registry.example/clawbox/tool-bridge@sha256:{'3' * 64}",
    }
    paths = renderer.render(tmp_path, images)

    assert {path.name for path in paths} == set(renderer.FILES)
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert not any(pattern.search(combined) for pattern in renderer.REPLACEMENTS)
    assert images["control"] in combined
    assert images["runtime"] in combined
    assert images["tool_bridge"] in combined


def test_renderer_rejects_mutable_image_reference():
    renderer = load_renderer()
    with pytest.raises(Exception, match="IMAGE@sha256"):
        renderer.immutable_image("registry.example/clawbox/control:latest")
