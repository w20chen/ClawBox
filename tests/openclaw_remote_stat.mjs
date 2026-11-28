// Run against an installed OpenClaw in a disposable Linux image.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const exec = promisify(execFile);
const require = createRequire(import.meta.url);
const [dist, patch] = process.argv.slice(2);
const { patchRemoteStat, patchSandboxRead, patchOpenClaw } = require(patch);
const sources = [];
const readSources = [];
for (const name of await fs.readdir(dist)) {
  if (!name.endsWith('.js')) continue;
  const text = await fs.readFile(path.join(dist, name), 'utf8');
  if (text.includes('var RemoteShellSandboxFsBridge = class {')) sources.push(text);
  if (text.includes('function createSandboxedReadTool(params) {')) readSources.push(text);
}
assert.equal(sources.length, 1);
assert.equal(readSources.length, 1);
const baselineStatCalls = sources[0].includes('clawbox-batched-stat-v1') ? 2 : 4;
const baselineContentCalls = readSources[0].includes('clawbox-invocation-read-v1') ? 1 : 2;
const updated = patchRemoteStat(sources[0]);
assert.equal(patchRemoteStat(updated), updated, 'patch must be idempotent');
const base = await fs.mkdtemp(path.join(os.tmpdir(), 'clawbox-stat-'));
const fastRoot = await fs.mkdtemp('/clawbox-read-');
const workspace = path.join(base, 'workspace');
await fs.mkdir(workspace);
await fs.writeFile(path.join(workspace, 'ordinary.txt'), 'same content\n');
await fs.writeFile(path.join(workspace, 'spaced name | unicode-文件.txt'), 'unicode\n');
await fs.writeFile(path.join(workspace, 'line\nbreak.txt'), 'newline\n');
await fs.writeFile(path.join(base, 'outside.txt'), 'outside\n');
await fs.mkdir(path.join(workspace, 'directory'));
await fs.symlink('ordinary.txt', path.join(workspace, 'inside-link'));
await fs.symlink('../outside.txt', path.join(workspace, 'outside-link'));
await fs.symlink('missing-target', path.join(workspace, 'dangling-link'));
await fs.writeFile(path.join(workspace, 'hard.txt'), 'hardlink\n');
await fs.link(path.join(workspace, 'hard.txt'), path.join(workspace, 'hard-alias.txt'));
await fs.writeFile(path.join(fastRoot, 'ordinary.txt'), 'same content\n');
await fs.writeFile(path.join(fastRoot, 'empty.txt'), '');
await fs.mkdir(path.join(fastRoot, 'directory'));
await fs.symlink('ordinary.txt', path.join(fastRoot, 'inside-link'));
await fs.symlink(path.join(base, 'outside.txt'), path.join(fastRoot, 'outside-link'));
await fs.symlink(base, path.join(fastRoot, 'outside-parent'));
await fs.symlink('missing-target', path.join(fastRoot, 'dangling-link'));
await fs.writeFile(path.join(fastRoot, 'hard.txt'), 'hardlink\n');
await fs.link(path.join(fastRoot, 'hard.txt'), path.join(fastRoot, 'hard-alias.txt'));

const cases = ['ordinary.txt', 'spaced name | unicode-文件.txt', 'line\nbreak.txt',
  'directory', '.', 'missing.txt', 'missing/directory/file', 'inside-link',
  'outside-link', 'dangling-link', 'hard.txt'];
const reports = [];
const readResults = [];
const fastReadResults = [];
const modulePaths = [];
try {
  for (const [variant, source] of [['baseline', sources[0]], ['candidate', updated]]) {
    const modulePath = path.join(dist, `clawbox-stat-${process.pid}-${variant}.mjs`);
    modulePaths.push(modulePath);
    await fs.writeFile(modulePath, source + '\nexport { RemoteShellSandboxFsBridge };\n');
    const { RemoteShellSandboxFsBridge } = await import(pathToFileURL(modulePath));
    let calls = 0;
    const runtime = {
      remoteWorkspaceDir: workspace, remoteAgentWorkspaceDir: workspace,
      async runRemoteShellScript(params) {
        calls++;
        try {
          const result = await exec('/bin/sh', ['-c', params.script, 'openclaw-sandbox-fs', ...(params.args ?? [])],
            { encoding: 'buffer', signal: params.signal });
          return { ...result, code: 0 };
        } catch (error) {
          if (error.name === 'AbortError') throw error;
          if (params.allowFailure) return { stdout: error.stdout, stderr: error.stderr, code: error.code };
          throw Object.assign(new Error('remote-command-failed'), {
            code: error.code, stdout: error.stdout, stderr: error.stderr });
        }
      },
    };
    const bridge = new RemoteShellSandboxFsBridge({ workspaceDir: workspace,
      agentWorkspaceDir: workspace, workspaceAccess: 'rw' }, runtime);
    const readModule = path.join(dist, `clawbox-read-integrated-${process.pid}-${variant}.mjs`);
    modulePaths.push(readModule);
    await fs.writeFile(readModule, (variant === 'baseline' ? readSources[0] : patchSandboxRead(readSources[0])) +
      '\nexport { createSandboxedReadTool };\n');
    const { createSandboxedReadTool } = await import(pathToFileURL(readModule));
    const readTool = createSandboxedReadTool({ root: workspace, bridge });
    calls = 0;
    readResults.push(await readTool.execute('controlled-read', { path: 'ordinary.txt' }));
    assert.equal(calls, variant === 'baseline' ? baselineStatCalls + baselineContentCalls : 3);
    const readCalls = calls;
    const fastBridge = new RemoteShellSandboxFsBridge({ workspaceDir: fastRoot,
      agentWorkspaceDir: fastRoot, workspaceAccess: 'rw' }, { ...runtime,
        remoteWorkspaceDir: fastRoot, remoteAgentWorkspaceDir: fastRoot });
    const fastTool = createSandboxedReadTool({ root: fastRoot, bridge: fastBridge });
    const nestedRoot = path.join(fastRoot, 'outside-parent', 'workspace');
    const nestedBridge = new RemoteShellSandboxFsBridge({ workspaceDir: nestedRoot,
      agentWorkspaceDir: nestedRoot, workspaceAccess: 'rw' }, { ...runtime,
        remoteWorkspaceDir: nestedRoot, remoteAgentWorkspaceDir: nestedRoot });
    const nestedTool = createSandboxedReadTool({ root: nestedRoot, bridge: nestedBridge });
    calls = 0;
    await assert.rejects(nestedTool.execute('nested-symlink-root', { path: 'ordinary.txt' }),
      /escapes allowed mounts/);
    assert.equal(calls, variant === 'baseline' ? baselineStatCalls / 2 : 1);
    if (variant === 'candidate') {
      assert.equal(nestedBridge.canReadWithoutStat({ filePath: 'ordinary.txt', cwd: nestedRoot }), false);
      for (const code of [125, 124, 255, 'ABORT_ERR', 'EPIPE']) {
        let attempts = 0;
        const failure = Object.assign(new Error(`controlled-failure-${code}`), { code });
        const failedBridge = new RemoteShellSandboxFsBridge({ workspaceDir: fastRoot,
          agentWorkspaceDir: fastRoot, workspaceAccess: 'rw' }, { ...runtime,
            remoteWorkspaceDir: fastRoot, remoteAgentWorkspaceDir: fastRoot,
            async runRemoteShellScript() { attempts++; throw failure; } });
        const failedTool = createSandboxedReadTool({ root: fastRoot, bridge: failedBridge });
        await assert.rejects(failedTool.execute('failed-read', { path: 'ordinary.txt' }),
          new RegExp(`controlled-failure-${code}`));
        assert.equal(attempts, 1, `must not retry failure ${code}`);
      }
    }
    const fastOutcomes = [];
    for (const filePath of ['ordinary.txt', 'empty.txt', 'directory', '.', 'missing.txt',
      'inside-link', 'outside-link', 'outside-parent/outside.txt', 'dangling-link', 'hard.txt']) {
      calls = 0;
      let result;
      try { result = { value: await fastTool.execute('controlled-read', { path: filePath }) }; }
      catch (error) { result = { error: error.message }; }
      if (filePath === 'ordinary.txt' || filePath === 'empty.txt') {
        assert.ok(result.value);
        assert.equal(calls, variant === 'baseline' ? baselineStatCalls + baselineContentCalls : 1);
      }
      fastOutcomes.push({ filePath, calls, result });
    }
    fastReadResults.push(fastOutcomes);
    const outcomes = [];
    for (const filePath of cases) {
      calls = 0;
      let result;
      try { result = { value: await bridge.stat({ filePath, cwd: workspace }) }; }
      catch (error) { result = { error: error.message }; }
      outcomes.push({ filePath, calls, result });
    }
    calls = 0;
    const cancelled = new AbortController(); cancelled.abort();
    await assert.rejects(bridge.stat({ filePath: 'ordinary.txt', cwd: workspace,
      signal: cancelled.signal }), { name: 'AbortError' });
    assert.equal(calls, 1);
    reports.push({ variant, outcomes, cancellation: 'passed', nestedSymlinkRoot: 'passed',
      fatalFailures: variant === 'candidate' ? 5 : 0, integratedReadCalls: readCalls,
      guardedReadCalls: fastOutcomes.map(({ filePath, calls }) => ({ filePath, calls })) });
  }
  assert.deepEqual(readResults[1], readResults[0], 'integrated Read output must match');
  for (let i = 0; i < fastReadResults[0].length; i++) {
    assert.deepEqual(fastReadResults[1][i].result, fastReadResults[0][i].result,
      fastReadResults[0][i].filePath);
  }
  for (let i = 0; i < cases.length; i++) {
    assert.deepEqual(reports[1].outcomes[i].result, reports[0].outcomes[i].result, cases[i]);
  }
  for (const filePath of ['ordinary.txt', 'directory', '.', 'inside-link']) {
    assert.equal(reports[0].outcomes.find(row => row.filePath === filePath).calls, baselineStatCalls);
    assert.equal(reports[1].outcomes.find(row => row.filePath === filePath).calls, 2);
  }
  assert.match(reports[1].outcomes.find(row => row.filePath === 'outside-link').result.error,
    /escapes allowed mounts/);
  assert.match(reports[1].outcomes.find(row => row.filePath === 'hard.txt').result.error,
    /Hardlinked path/);
  assert.equal(reports[1].outcomes.find(row => row.filePath === 'missing.txt').calls, 1);
  // Exercise the complete image patch, not just the isolated stat replacement.
  // This test must run in a disposable installation because it patches dist.
  const log = console.log;
  let changed;
  try {
    console.log = () => {};
    changed = patchOpenClaw(dist);
    assert.deepEqual(patchOpenClaw(dist), [], 'full image patch must be idempotent');
  } finally {
    console.log = log;
  }
  for (const name of changed) await exec(process.execPath, ['--check', path.join(dist, name)]);
  console.log(JSON.stringify({ passed: true, cases: cases.length, reports,
    fullPatch: { changed, idempotent: true, syntax: 'passed' } }));
} finally {
  for (const file of modulePaths) await fs.unlink(file);
  await fs.rm(base, { recursive: true });
  await fs.rm(fastRoot, { recursive: true });
}
