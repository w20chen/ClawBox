#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const globalRoot = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
const dist = path.join(globalRoot, "openclaw", "dist");
if (!fs.existsSync(dist)) throw new Error(`OpenClaw dist directory is missing: ${dist}`);

let rootPatchCount = 0;
let skillsPatchCount = 0;
const changed = [];
for (const name of fs.readdirSync(dist)) {
  if (!name.endsWith(".js")) continue;
  const file = path.join(dist, name);
  const source = fs.readFileSync(file, "utf8");
  let next = source.replace(
    /remoteWorkspaceDir:\s*path\.posix\.join\(runtimeRootDir,\s*"workspace"\)/g,
    "remoteWorkspaceDir: workspaceRoot",
  );
  if (next.includes("remoteWorkspaceDir: workspaceRoot")) rootPatchCount += 1;

  if (next.includes("async refreshRemoteSkillsWorkspace(session)")) {
    skillsPatchCount += 1;
    if (!next.includes("this.skillsRefreshPromise = null;")) {
      next = next.replace(
        "this.ensurePromise = null;\n\t\tthis.refreshedSkillsForNextExecWorkdir = null;",
        "this.ensurePromise = null;\n\t\tthis.skillsRefreshPromise = null;\n\t\tthis.skillsRefreshComplete = false;\n\t\tthis.refreshedSkillsForNextExecWorkdir = null;",
      );
      next = next.replace(
        "async refreshRemoteSkillsWorkspace(session) {\n\t\tif (this.params.createParams.cfg.workspaceAccess",
        "async refreshRemoteSkillsWorkspace(session) {\n\t\tif (this.skillsRefreshComplete) return;\n\t\tif (this.skillsRefreshPromise) return await this.skillsRefreshPromise;\n\t\tthis.skillsRefreshPromise = this.refreshRemoteSkillsWorkspaceInner(session);\n\t\ttry {\n\t\t\tawait this.skillsRefreshPromise;\n\t\t\tthis.skillsRefreshComplete = true;\n\t\t} finally {\n\t\t\tthis.skillsRefreshPromise = null;\n\t\t}\n\t}\n\tasync refreshRemoteSkillsWorkspaceInner(session) {\n\t\tif (this.params.createParams.cfg.workspaceAccess",
      );
    } else if (!next.includes("this.skillsRefreshComplete = true;")) {
      next = next.replace(
        "this.skillsRefreshPromise = null;\n\t\tthis.refreshedSkillsForNextExecWorkdir = null;",
        "this.skillsRefreshPromise = null;\n\t\tthis.skillsRefreshComplete = false;\n\t\tthis.refreshedSkillsForNextExecWorkdir = null;",
      );
      next = next.replace(
        "async refreshRemoteSkillsWorkspace(session) {\n\t\tif (this.skillsRefreshPromise)",
        "async refreshRemoteSkillsWorkspace(session) {\n\t\tif (this.skillsRefreshComplete) return;\n\t\tif (this.skillsRefreshPromise)",
      );
      next = next.replace(
        "\t\t\tawait this.skillsRefreshPromise;\n\t\t} finally {\n\t\t\tthis.skillsRefreshPromise = null;",
        "\t\t\tawait this.skillsRefreshPromise;\n\t\t\tthis.skillsRefreshComplete = true;\n\t\t} finally {\n\t\t\tthis.skillsRefreshPromise = null;",
      );
    }
    if (!next.includes("this.skillsRefreshComplete = false;") ||
        !next.includes("this.skillsRefreshComplete = true;")) {
      throw new Error(`unexpected OpenClaw SSH skills bundle shape: ${name}`);
    }
  }
  if (next !== source) {
    fs.writeFileSync(file, next);
    changed.push(name);
  }
}

if (rootPatchCount < 1) throw new Error("OpenClaw SSH workspace-root patch target is missing");
if (skillsPatchCount !== 1) {
  throw new Error(`expected one OpenClaw SSH skills bundle, found ${skillsPatchCount}`);
}
console.log(`OpenClaw SSH backend verified; changed: ${changed.join(", ") || "none"}`);
