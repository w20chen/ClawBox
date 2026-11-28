// Run only against a disposable installed OpenClaw; no network/model requests.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const [dist, patch] = process.argv.slice(2);
const { patchSandboxRead } = createRequire(import.meta.url)(patch);
const sources = [];
for (const name of await fs.readdir(dist)) {
  if (!name.endsWith('.js')) continue;
  const source = await fs.readFile(path.join(dist, name), 'utf8');
  if (source.includes('function createSandboxedReadTool(params) {')) sources.push(source);
}
assert.equal(sources.length, 1);
const baselineContentCalls = sources[0].includes('clawbox-invocation-read-v1') ? 1 : 2;
const updated = patchSandboxRead(sources[0]);
assert.equal(patchSandboxRead(updated), updated);
const modules = [];
const reports = [];
try {
  for (const [variant, source] of [['baseline', sources[0]], ['candidate', updated]]) {
    const modulePath = path.join(dist, `clawbox-read-${process.pid}-${variant}.mjs`);
    modules.push(modulePath);
    await fs.writeFile(modulePath, source + '\nexport { createSandboxedReadTool };\n');
    const { createSandboxedReadTool } = await import(pathToFileURL(modulePath));
    let calls = 0;
    const files = new Map([
      ['/workspace/text.txt', Buffer.from('first version\n')],
      ['/workspace/unicode.txt', Buffer.from('中文内容\n')],
      ['/workspace/lines.txt', Buffer.from(Array.from({ length: 100 }, (_, i) => `line ${i}`).join('\n'))],
      ['/workspace/image.png', Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==', 'base64')],
      ['/workspace/read-denied.txt', Buffer.from('unreadable')],
    ]);
    const bridge = {
      resolvePath: ({ filePath }) => ({ containerPath: filePath }),
      async stat({ filePath }) {
        filePath = path.resolve('/workspace', filePath);
        if (filePath === '/workspace/stat-denied.txt') throw new Error('controlled-stat-denied');
        return files.has(filePath) ? { type: 'file', size: files.get(filePath).length, mtimeMs: 0 } : null;
      },
      async readFile({ filePath }) {
        filePath = path.resolve('/workspace', filePath);
        calls++;
        if (filePath === '/workspace/read-denied.txt') throw new Error('controlled-read-denied');
        const content = files.get(filePath);
        await new Promise(resolve => setTimeout(resolve, 1));
        if (!content) throw new Error('controlled-missing');
        return Buffer.from(content);
      },
    };
    const tool = createSandboxedReadTool({ root: '/workspace', bridge });
    const outcomes = [];
    const run = async (name, args, signal) => {
      calls = 0;
      let result;
      try { result = { value: await tool.execute(name, args, signal) }; }
      catch (error) { result = { error: error.message }; }
      const row = { name, calls, result };
      outcomes.push(row);
      return row;
    };
    await run('text', { path: 'text.txt' });
    files.set('/workspace/text.txt', Buffer.from('second version\n'));
    const changed = await run('updated', { path: 'text.txt' });
    assert.match(JSON.stringify(changed.result), /second version/);
    await run('unicode', { path: 'unicode.txt' });
    await run('paging', { path: 'lines.txt', offset: 5, limit: 7 });
    await run('image', { path: 'image.png' });
    await run('missing', { path: 'missing.txt' });
    await run('stat-denied', { path: 'stat-denied.txt' });
    await run('read-denied', { path: 'read-denied.txt' });
    await run('invalid', {});
    const controller = new AbortController(); controller.abort();
    await run('cancelled', { path: 'text.txt' }, controller.signal);
    calls = 0;
    const concurrent = await Promise.all([tool.execute('parallel-1', { path: 'text.txt' }),
      tool.execute('parallel-2', { path: 'text.txt' })]);
    assert.deepEqual(concurrent[0], concurrent[1]);
    assert.equal(calls, variant === 'baseline' ? 2 * baselineContentCalls : 2, 'concurrent invocations must read independently');
    reports.push({ variant, outcomes, concurrentReads: calls });
  }
  for (let i = 0; i < reports[0].outcomes.length; i++) {
    const original = reports[0].outcomes[i], candidate = reports[1].outcomes[i];
    assert.deepEqual(candidate.result, original.result, original.name);
    if (['text', 'updated', 'unicode', 'paging', 'image'].includes(original.name)) {
      assert.equal(original.calls, baselineContentCalls, original.name);
      assert.equal(candidate.calls, 1, original.name);
      assert.ok(candidate.result.value, original.name);
    }
  }
  // Output metadata only, never tool contents.
  console.log(JSON.stringify({ passed: true, cases: reports[0].outcomes.length,
    reports: reports.map(report => ({ ...report,
      outcomes: report.outcomes.map(({ name, calls }) => ({ name, calls })) })) }));
} finally {
  for (const file of modules) await fs.unlink(file);
}
