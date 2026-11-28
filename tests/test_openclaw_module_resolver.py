"""Exercise Node resolution, including boundaries of the packaged ESM shortcut."""
import json
import os
import shutil
import subprocess

import pytest

from clawbox.experiments.openclaw_driver import openclaw_auth_preload


@pytest.mark.parametrize("layout", ["esm", "dist-commonjs", "dist-untyped", "symlink"])
def test_fast_resolution_preserves_node_import_semantics(tmp_path, layout):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for actual module resolution")
    package = tmp_path / "node_modules" / "openclaw"
    dist = package / "dist"
    dist.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({
        "name": "openclaw", "type": "module",
        "imports": {"#chunk": "./dist/chunk.js"},
        "exports": {"./chunk": "./dist/chunk.js",
                    "./plugin-sdk/provider-auth": "./auth.mjs"},
    }))
    (package / "auth.mjs").write_text("export const upsertAuthProfileWithLock = async () => ({});")
    if layout.startswith("dist-"):
        (dist / "package.json").write_text(json.dumps(
            {"type": "commonjs"} if layout == "dist-commonjs" else {}
        ))
    (dist / "chunk.js").write_text(
        "export default {value: 7};" if layout in {"esm", "symlink"}
        else "module.exports = {value: 7};"
    )
    (dist / "data.json").write_text('{"value":11}')
    (dist / "plain.cjs").write_text('module.exports = {value:13};')
    nested = dist / "nested"
    nested.mkdir()
    (nested / "package.json").write_text('{"type":"commonjs"}')
    (nested / "chunk.js").write_text('module.exports = {value:17};')
    symlink_probe = ""
    if layout == "symlink":
        try:
            (dist / "link.js").symlink_to(nested / "chunk.js")
        except OSError as error:
            pytest.skip(f"Symlink creation unavailable: {error}")
        symlink_probe = "if ((await import('./dist/link.js')).default !== nested.default) throw Error('symlink identity');"
    agent = package / "openclaw.mjs"
    agent.write_text("""
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const a = await import('./dist/chunk.js');
assert.equal(a.default.value, 7);
assert.equal(a, await import('./dist/../dist/chunk.js'));
assert.equal(a, await import(new URL('./dist/chunk.js', import.meta.url)));
assert.equal(a, await import('openclaw/chunk'));
assert.equal(a, await import('#chunk'));
assert.equal(await import('./dist/chunk.js?q=1'), await import('./dist/chunk.js?q=1'));
assert.notEqual(a, await import('./dist/chunk.js?q=1'));
assert.equal((await import('./dist/data.json', {with: {type: 'json'}})).default.value, 11);
assert.equal((await import('./dist/plain.cjs')).default.value, 13);
const nested = await import('./dist/nested/chunk.js');
assert.equal(nested.default.value, 17);
const errors = [];
for (const name of ['./dist/missing.js', './dist/nested', './dist%2Fchunk.js', 'openclaw/private']) {
  try { await import(name); throw Error('unexpected import success'); }
  catch (error) { errors.push(error.code); }
}
assert.deepEqual(errors, ['ERR_MODULE_NOT_FOUND', 'ERR_UNSUPPORTED_DIR_IMPORT',
                         'ERR_INVALID_MODULE_SPECIFIER', 'ERR_PACKAGE_PATH_NOT_EXPORTED']);
try {
  await import('./dist/chunk.js', {with: {type: 'json'}});
  throw Error('unexpected attribute success');
} catch (error) { assert.equal(error.code, 'ERR_IMPORT_ATTRIBUTE_TYPE_INCOMPATIBLE'); }
""" + symlink_probe + "\nconsole.log('semantic checks passed');")
    environment = dict(os.environ, OPENCLAW_STATE_DIR=str(tmp_path / "state"),
                       CLAWTUNE_RUNTIME_ID="resolver-test")
    environment.pop("NODE_OPTIONS", None)
    for enabled in (False, True):
        preload = tmp_path / f"auth-{enabled}.mjs"
        preload.write_text(openclaw_auth_preload(fast_module_resolution=enabled))
        result = subprocess.run([node, "--import", preload.as_uri(), str(agent)],
                                env=environment, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "semantic checks passed"
