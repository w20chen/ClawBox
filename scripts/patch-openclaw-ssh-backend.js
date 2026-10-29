#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

function patchOpenClaw(dist) {
if (!fs.existsSync(dist)) throw new Error(`OpenClaw dist directory is missing: ${dist}`);

let rootPatchCount = 0;
let skillsPatchCount = 0;
let noOpEditPatchCount = 0;
let sandboxPreflightPatchCount = 0;
let cliCompactionPatchCount = 0;
let agentCompactionPatchCount = 0;
const changed = [];
for (const name of fs.readdirSync(dist)) {
  if (!name.endsWith(".js")) continue;
  const file = path.join(dist, name);
  const source = fs.readFileSync(file, "utf8");
  let next = source.replace(
    /remoteWorkspaceDir:\s*path\.posix\.join\(runtimeRootDir,\s*"workspace"\)/g,
    "remoteWorkspaceDir: workspaceRoot",
  );
  // Replay has recorded model responses only. A generated summary is an
  // unrecorded model call and can consume a normal response or abort the run.
  // Leave live inference's context management unchanged.
  for (const [signature, guard, kind] of [
    ['async function runCliTurnCompactionLifecycle(params) {',
     '\n\tif (process.env.CLAWBOX_REPLAY_NO_AUTO_COMPACTION === "1") return params.sessionEntry;', 'cli'],
    ['function shouldDisableAgentAutoCompaction(params) {',
     '\n\tif (process.env.CLAWBOX_REPLAY_NO_AUTO_COMPACTION === "1") return true;', 'agent'],
  ]) {
    if (!next.includes(signature)) continue;
    if (!next.includes(signature + guard)) next = next.replace(signature, signature + guard);
    if (kind === 'cli') cliCompactionPatchCount += 1;
    else agentCompactionPatchCount += 1;
  }
  if (next.includes("remoteWorkspaceDir: workspaceRoot")) rootPatchCount += 1;

  // A recorded no-op edit is still a completed tool call. OpenClaw 2026.7.1-2
  // otherwise terminates the agent turn here, leaving the remaining replay
  // responses unconsumed. Preserve termination for every other tool result.
  next = next.replace(
    /(\.\.\.textResult\(`No changes made to \$\{path\}\. The replacement text is identical to the original\.`, void 0\),\s*terminate: )(true|false)(?=\s*\n\s*\};)/g,
    (_match, prefix) => {
      noOpEditPatchCount += 1;
      return `${prefix}false`;
    },
  );

  // ClawBox explicitly grants full/off command execution inside its Tool VM.
  // Honor that same policy for the sandbox host; restricted/approval modes
  // retain OpenClaw's script preflight.
  const sandboxPreflight = 'return (params.host === "gateway" || params.host === "sandbox") && params.security === "full" && params.ask === "off";';
  if (next.includes("function shouldSkipExecScriptPreflight(params) {")) {
    next = next.replace(
      'return params.host === "gateway" && params.security === "full" && params.ask === "off";',
      sandboxPreflight,
    );
    if (!next.includes(sandboxPreflight)) {
      throw new Error(`unexpected OpenClaw script preflight shape: ${name}`);
    }
    sandboxPreflightPatchCount += 1;
  }

  if (next.includes("async refreshRemoteSkillsWorkspace(session)")) {
    skillsPatchCount += 1;
    const skillsSignature = "async refreshRemoteSkillsWorkspace(session) {";
    const replaySkillsGuard = '\n\t\tif (process.env.CLAWBOX_REPLAY_NO_REMOTE_SKILLS === "1") { this.skillsRefreshComplete = true; return; }';
    // Finish upgrading an older promise-only synchronization patch before
    // adding the replay guard. The guard itself assigns the completed flag,
    // so inserting it first would make the upgrade test below a false match.
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
    if (!next.includes(skillsSignature + replaySkillsGuard)) {
      next = next.replace(skillsSignature, skillsSignature + replaySkillsGuard);
    }
    const missingSkillsParts = [
      ["replay guard", "CLAWBOX_REPLAY_NO_REMOTE_SKILLS"],
      ["constructor state", "this.skillsRefreshComplete = false;"],
      ["successful completion", "this.skillsRefreshComplete = true;"],
    ].filter(([, signature]) => !next.includes(signature)).map(([label]) => label);
    if (missingSkillsParts.length) {
      throw new Error(`unexpected OpenClaw SSH skills bundle shape: ${name}; missing ${missingSkillsParts.join(", ")}`);
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
if (noOpEditPatchCount !== 1) {
  throw new Error(`expected one OpenClaw no-op edit result, found ${noOpEditPatchCount}`);
}
if (sandboxPreflightPatchCount !== 1) {
  throw new Error(`expected one OpenClaw script preflight policy, found ${sandboxPreflightPatchCount}`);
}
if (cliCompactionPatchCount !== 1 || agentCompactionPatchCount !== 1) {
  throw new Error(`expected one CLI and agent compaction guard, found ${cliCompactionPatchCount}/${agentCompactionPatchCount}`);
}
console.log(`OpenClaw SSH backend and no-op edit continuation verified; changed: ${changed.join(", ") || "none"}`);
return changed;
}

module.exports = { patchOpenClaw };
if (require.main === module) {
  const globalRoot = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
  patchOpenClaw(path.join(globalRoot, "openclaw", "dist"));
}
