"""Tests for the runtime patch-collection logic (committed + working-tree).

runtime-entrypoint.sh captures the agent's fix as ``git diff`` (working tree)
PLUS ``git diff <baseline HEAD> HEAD`` when the agent committed changes.  This
test reproduces that exact sh script (the heredoc body) and asserts the three
scenarios: both committed+uncommitted captured, no-baseline fallback, and
HEAD==baseline.  Requires git + a POSIX sh (Git for Windows bundles both).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

COLLECT_SCRIPT = """#!/bin/sh
cd /testbed || exit 0
git diff --binary --no-ext-diff
current="$(git rev-parse HEAD 2>/dev/null || true)"
baseline_head="__BASELINE_HEAD__"
if [ -n "$current" ] && [ -n "$baseline_head" ] && [ "$current" != "$baseline_head" ]; then
  printf '\\n# committed changes since baseline %s\\n' "$baseline_head"
  git diff --binary --no-ext-diff "$baseline_head" HEAD
fi
"""


def _find_sh() -> str:
    found = shutil.which("sh")
    if found:
        return found
    git = shutil.which("git")
    if git:
        candidate = str(Path(git).resolve().parents[1] / "usr" / "bin" / "sh.exe")
        if Path(candidate).exists():
            return candidate
    return ""


def _posix_path(repo: Path) -> str:
    raw = str(repo).replace("\\", "/")
    if len(raw) >= 2 and raw[1] == ":":
        return "/" + raw[0].lower() + raw[2:]
    return raw


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _simulate_patch(repo: Path, baseline_head: str) -> str:
    sh = _find_sh()
    script = COLLECT_SCRIPT.replace("__BASELINE_HEAD__", baseline_head)
    script = script.replace("cd /testbed", f"cd {_posix_path(repo)}")
    result = subprocess.run(
        [sh, "-s"], cwd=str(repo), input=script, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture()
def repo(tmp_path):
    sh = _find_sh()
    if not sh or shutil.which("git") is None:
        pytest.skip("git + POSIX sh required for patch-collection test")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "file.txt").write_text("baseline\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "baseline")
    return tmp_path


def test_patch_captures_committed_and_uncommitted(repo):
    baseline = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "file.txt").write_text("baseline\nuncommitted-change\n", encoding="utf-8")
    (repo / "committed.txt").write_text("committed-change\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "agent fix")
    patch = _simulate_patch(repo, baseline)
    assert "uncommitted-change" in patch
    assert "committed-change" in patch
    assert "# committed changes since baseline" in patch


def test_patch_falls_back_without_baseline(repo):
    (repo / "file.txt").write_text("baseline\nuncommitted-only\n", encoding="utf-8")
    patch = _simulate_patch(repo, "")
    assert "uncommitted-only" in patch
    assert "# committed changes since baseline" not in patch


def test_patch_only_working_tree_when_head_equals_baseline(repo):
    baseline = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "file.txt").write_text("baseline\nplus-new\n", encoding="utf-8")
    patch = _simulate_patch(repo, baseline)
    assert "plus-new" in patch
    assert "# committed changes since baseline" not in patch
