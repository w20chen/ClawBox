"""Preload optimization for the immutable installed OpenClaw ESM bundle."""

# Node repeatedly deserializes OpenClaw's large package exports map just to
# discover the format of ordinary sibling chunks. Restrict the shortcut to
# regular direct children of dist under the verified module package. All
# package resolution and exceptional filesystem layouts stay with Node.
OPENCLAW_MODULE_RESOLVER = """\
import { readFileSync as readModuleMetadata, lstatSync as statModuleEntry } from 'node:fs';
import { registerHooks } from 'node:module';
import { dirname as moduleDirname, extname as moduleExtension } from 'node:path';
import { fileURLToPath as moduleFilePath } from 'node:url';

const moduleRoot = moduleDirname(realpathSync(process.argv[1]));
const moduleDist = join(moduleRoot, 'dist');
let moduleDistIsESM = false;
try {
  const metadata = JSON.parse(readModuleMetadata(join(moduleRoot, 'package.json'), 'utf8'));
  moduleDistIsESM = metadata.type === 'module' && statModuleEntry(moduleDist).isDirectory();
  // Even a package.json without a type overrides the parent package scope.
  try {
    statModuleEntry(join(moduleDist, 'package.json'));
    moduleDistIsESM = false;
  } catch (error) {
    if (error.code !== 'ENOENT') moduleDistIsESM = false;
  }
} catch {
  // Let Node report missing/invalid metadata through its normal resolver.
}
if (moduleDistIsESM) {
  const moduleFiles = new Set();
  registerHooks({
    resolve(specifier, context, nextResolve) {
      if (specifier.startsWith('./') || specifier.startsWith('../') ||
          specifier.startsWith('file:') || specifier.startsWith('/')) {
        let url, file;
        try {
          url = new URL(specifier, context.parentURL);
          file = moduleFilePath(url);
        } catch { return nextResolve(specifier, context); }
        if (moduleDirname(file) === moduleDist && moduleExtension(file) === '.js') {
          if (!moduleFiles.has(file)) {
            try {
              // Symlinks may target another package with a different type.
              if (!statModuleEntry(file).isFile()) return nextResolve(specifier, context);
            } catch { return nextResolve(specifier, context); }
            moduleFiles.add(file);
          }
          return { url: url.href, format: 'module', shortCircuit: true };
        }
      }
      return nextResolve(specifier, context);
    },
  });
}
"""
