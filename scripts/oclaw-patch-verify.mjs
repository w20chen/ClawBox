// Verify + preview the openclaw sandbox-root patch against a dist bundle.
// Usage: node oclaw-patch-verify.mjs <distdir> [--write]
import fs from "fs";
import path from "path";

const distDir = process.argv[2] ?? "package/dist";
const write = process.argv.includes("--write");
const re = /remoteWorkspaceDir:\s*path\.posix\.join\(runtimeRootDir,\s*"workspace"\)/g;

let patched = [];
for (const f of fs.readdirSync(distDir)) {
  if (!f.endsWith(".js")) continue;
  const p = path.join(distDir, f);
  const s = fs.readFileSync(p, "utf8");
  const next = s.replace(re, "remoteWorkspaceDir: workspaceRoot");
  if (next !== s) {
    patched.push(f);
    if (write) fs.writeFileSync(p, next);
  }
}
if (!patched.length) {
  console.error("NO MATCH: bundle did not contain the target pattern");
  process.exit(1);
}
console.log(`MATCH (${write ? "written" : "dry-run"}): ${patched.join(", ")}`);
for (const f of patched) {
  const p = path.join(distDir, f);
  const s = fs.readFileSync(p, "utf8");
  const i = s.indexOf("function resolveSshRuntimePaths");
  console.log(`--- ${f} resolveSshRuntimePaths ---`);
  console.log(s.slice(i, i + 600));
}
