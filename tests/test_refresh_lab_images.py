from __future__ import annotations

import importlib.util
import json
from argparse import Namespace
from pathlib import Path


def _module():
    path = Path("scripts/refresh-lab-images.py").resolve()
    spec = importlib.util.spec_from_file_location("refresh_lab_images", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_flattened_base_preserves_runtime_config_without_layer_parent(monkeypatch):
    module = _module()
    config = {
        "Config": {
            "Env": ["PATH=/usr/bin:/bin", "XDG_CACHE_HOME=/old/cache"],
            "Labels": {"io.clawbox.kernel": "sha256:test"},
            "WorkingDir": "/workspace",
        },
    }
    monkeypatch.setattr(
        module.subprocess,
        "check_output",
        lambda *args, **kwargs: json.dumps([config]),
    )

    rendered = module.flattened_base("registry/base@sha256:abc")

    assert rendered.startswith(
        "FROM registry/base@sha256:abc AS previous\n"
        "FROM scratch\n"
        "COPY --from=previous / /\n"
    )
    assert 'ENV PATH="/usr/bin:/bin"' in rendered
    assert 'ENV XDG_CACHE_HOME="/old/cache"' in rendered
    assert 'LABEL io.clawbox.kernel="sha256:test"' in rendered
    assert 'WORKDIR "/workspace"' in rendered
    assert rendered.endswith("USER root\n")


def test_saved_image_build_inputs_make_repeat_build_self_contained(monkeypatch):
    module = _module()
    monkeypatch.setenv("CLAWBOX_GO", "/environment/go")
    args = Namespace(
        registry=None, go=None, kernel_source=None, kernel_build=None,
        direct_network=None,
    )
    profile = {"image_build": {
        "registry": "registry.example/clawbox",
        "go": "/saved/go",
        "kernel_source": "/saved/source",
        "kernel_build": "/saved/build",
        "direct_network": True,
    }}

    assert module.resolve_build_inputs(args, profile) == profile["image_build"]


def test_explicit_image_build_inputs_override_saved_values():
    module = _module()
    args = Namespace(
        registry="new/registry", go="new-go", kernel_source="new-source",
        kernel_build="new-build", direct_network=False,
    )

    resolved = module.resolve_build_inputs(args, {"image_build": {
        "registry": "old/registry", "go": "old-go", "direct_network": True,
    }})

    assert resolved == {
        "registry": "new/registry", "go": "new-go",
        "kernel_source": "new-source", "kernel_build": "new-build",
        "direct_network": False,
    }


def test_linux_guest_scripts_are_normalized_to_lf(tmp_path):
    module = _module()
    script = tmp_path / "entrypoint.py"
    script.write_bytes(b"#!/usr/bin/env python3\r\nprint('ok')\r\n")

    module.normalize_unix_script(script)

    assert script.read_bytes() == b"#!/usr/bin/env python3\nprint('ok')\n"


def test_linux_guest_script_normalizer_rejects_binary_data(tmp_path):
    module = _module()
    script = tmp_path / "not-a-script"
    script.write_bytes(b"#!/bin/sh\n\0")

    try:
        module.normalize_unix_script(script)
    except ValueError as exc:
        assert "binary file" in str(exc)
    else:
        raise AssertionError("binary script was accepted")
