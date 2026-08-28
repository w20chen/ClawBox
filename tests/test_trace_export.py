from __future__ import annotations

import json
import urllib.error
from pathlib import Path

from clawbox.ingester import export


def test_export_traces_downloads_every_file_and_preserves_paths(tmp_path, monkeypatch):
    manifest = {
        "paths": {
            "run/events.jsonl": {"bytes": 8},
            "tool-bridge.jsonl": {"bytes": 7},
            "tool-resource/sample.json": {"bytes": 9},
        }
    }
    responses = {
        "http://archive/v1/archive/task%2Fone/traces": json.dumps(manifest).encode(),
        "http://archive/v1/archive/task%2Fone/traces/run/events.jsonl": b'{"a":1}\n',
        "http://archive/v1/archive/task%2Fone/traces/tool-bridge.jsonl": b'bridge\n',
        "http://archive/v1/archive/task%2Fone/traces/tool-resource/sample.json": b'{"cpu":1}',
    }
    monkeypatch.setattr(export, "_request", lambda url, token: responses[url])

    output = tmp_path / "traces"
    paths = export.export_traces("http://archive", "secret", "task/one", output)

    assert [path.relative_to(output).as_posix() for path in paths] == [
        "run/events.jsonl",
        "tool-bridge.jsonl",
        "tool-resource/sample.json",
    ]
    assert (output / "run/events.jsonl").read_bytes() == b'{"a":1}\n'
    assert (output / "tool-resource/sample.json").read_bytes() == b'{"cpu":1}'
    assert (output / "manifest.json").exists()


def test_export_traces_skips_conflicted_file_and_continues(tmp_path, monkeypatch, capsys):
    manifest = {
        "paths": {
            "broken.json": {"bytes": 9},
            "events.jsonl": {"bytes": 8},
        }
    }
    manifest_url = "http://archive/v1/archive/task/traces"

    def request(url: str, token: str) -> bytes:
        if url == manifest_url:
            return json.dumps(manifest).encode()
        if url.endswith("/broken.json"):
            raise urllib.error.HTTPError(url, 409, "Conflict", {}, None)
        return b'{"a":1}\n'

    monkeypatch.setattr(export, "_request", request)
    output = tmp_path / "traces"

    paths = export.export_traces("http://archive", "secret", "task", output)

    assert [path.relative_to(output).as_posix() for path in paths] == ["events.jsonl"]
    assert not (output / "broken.json").exists()
    assert "skipped incomplete archived trace file: broken.json" in capsys.readouterr().err
