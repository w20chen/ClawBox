from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


PATCH = Path(__file__).resolve().parents[1] / "scripts/patch-openclaw-ssh-backend.js"


def _apply(dist: Path) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the Runtime image patch")
    return subprocess.run(
        [node, "-e", f"require({json.dumps(str(PATCH))}).patchOpenClaw({json.dumps(str(dist))})"],
        text=True, capture_output=True, check=False,
    )


def _bundle(tmp_path: Path, *, no_op: bool = True) -> Path:
    # The SSH patch is already installed; test the actual no-op edit result
    # shape independently from unrelated workspace/skills transformations.
    source = '''
const ssh = { remoteWorkspaceDir: workspaceRoot };
class Backend {
\tconstructor() {
\t\tthis.ensurePromise = null;
\t\tthis.skillsRefreshPromise = null;
\t\tthis.refreshedSkillsForNextExecWorkdir = null;
\t}
\tasync refreshRemoteSkillsWorkspace(session) {
\t\tif (this.skillsRefreshPromise) return await this.skillsRefreshPromise;
\t\tthis.remoteSkillCopies = (this.remoteSkillCopies || 0) + 1;
\t\tthis.skillsRefreshPromise = Promise.resolve();
\t\ttry {
\t\t\tawait this.skillsRefreshPromise;
\t\t} finally {
\t\t\tthis.skillsRefreshPromise = null;
\t\t}
\t}
}
function textResult(text) { return { text }; }
function intentionalStop() { return { terminate: true }; }
async function runCliTurnCompactionLifecycle(params) {
  throw new Error('live compaction reached');
}
function shouldDisableAgentAutoCompaction(params) {
  return params.ownsCompaction === true;
}
function shouldSkipExecScriptPreflight(params) {
  return params.host === "gateway" && params.security === "full" && params.ask === "off";
}
'''
    if no_op:
        source += '''
function edit(path) {
  return {
    ...textResult(`No changes made to ${path}. The replacement text is identical to the original.`, void 0),
    terminate: true
  };
}
'''
    bundle = tmp_path / "sessions.js"
    bundle.write_text(source, encoding="utf-8", newline="\n")
    return bundle


def test_no_op_edit_continues_replay_and_preserves_other_termination(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    result = _apply(tmp_path)
    assert result.returncode == 0, result.stderr
    patched = bundle.read_text(encoding="utf-8")
    node = shutil.which("node")
    # Execute the patched result and the same batch-termination condition used
    # by OpenClaw, instead of asserting replacement text alone.
    executable = "const workspaceRoot='/testbed';\n" + patched + '''
const noOp = edit('/testbed/example.py');
const batchTerminates = results => results.every(result => result.terminate === true);
if (batchTerminates([noOp])) throw new Error('no-op ended replay');
if (!batchTerminates([intentionalStop()])) throw new Error('intentional termination lost');
for (const host of ['sandbox', 'gateway']) {
  if (!shouldSkipExecScriptPreflight({host, security:'full', ask:'off'}))
    throw new Error('explicit unrestricted execution was refused');
  for (const policy of [{security:'allowlist',ask:'off'}, {security:'full',ask:'on-miss'}, {security:'full',ask:'always'}]) {
    if (shouldSkipExecScriptPreflight({host, ...policy})) throw new Error('restricted preflight bypassed');
  }
}
if (shouldSkipExecScriptPreflight({host:'node',security:'full',ask:'off'}))
  throw new Error('unrelated execution host bypassed');
(async () => {
  process.env.CLAWBOX_REPLAY_NO_AUTO_COMPACTION = '1';
  const entry = {sessionFile:'large-recording'};
  if (await runCliTurnCompactionLifecycle({sessionEntry:entry}) !== entry)
    throw new Error('replay context changed');
  if (!shouldDisableAgentAutoCompaction({})) throw new Error('agent compaction still enabled');
  process.env.CLAWBOX_REPLAY_NO_AUTO_COMPACTION = '0';
  if (shouldDisableAgentAutoCompaction({})) throw new Error('live compaction disabled');
  if (!shouldDisableAgentAutoCompaction({ownsCompaction:true})) throw new Error('live guard lost');
  try { await runCliTurnCompactionLifecycle({}); throw new Error('live compaction skipped'); }
  catch (error) { if (error.message !== 'live compaction reached') throw error; }
  const replayBackend = new Backend();
  process.env.CLAWBOX_REPLAY_NO_REMOTE_SKILLS = '1';
  await replayBackend.refreshRemoteSkillsWorkspace({});
  if (replayBackend.remoteSkillCopies) throw new Error('replay copied Runtime skills into Tool workspace');
  const liveBackend = new Backend();
  process.env.CLAWBOX_REPLAY_NO_REMOTE_SKILLS = '0';
  await liveBackend.refreshRemoteSkillsWorkspace({});
  if (liveBackend.remoteSkillCopies !== 1) throw new Error('live skill refresh was disabled');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    check = subprocess.run([node, "-e", executable], text=True, capture_output=True)
    assert check.returncode == 0, check.stderr
    assert _apply(tmp_path).returncode == 0
    assert bundle.read_text(encoding="utf-8") == patched


def test_missing_no_op_edit_shape_fails_image_build(tmp_path: Path) -> None:
    _bundle(tmp_path, no_op=False)
    result = _apply(tmp_path)
    assert result.returncode != 0
    assert "expected one OpenClaw no-op edit result, found 0" in result.stderr
