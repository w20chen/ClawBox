#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

function patchRemoteStat(source) {
  const start = source.indexOf('var RemoteShellSandboxFsBridge = class {');
  if (start < 0) return null;
  const end = source.indexOf('\nfunction buildRemoteProtectedSkillMounts(', start);
  if (end < 0) throw new Error('OpenClaw remote filesystem class boundary is missing');
  let original = source.slice(start, end);
  if (!original.includes('canReadWithoutStat(params)')) {
    // The pinned read opens the root and every child without following links,
    // then checks the opened file's type/link count before returning any bytes.
    for (const required of ['DIR_FLAGS |= os.O_NOFOLLOW', 'READ_FLAGS |= os.O_NOFOLLOW',
      'file_stat.st_nlink > 1:', 'parent_fd = walk_dir(root_fd, sys.argv[3], False)']) {
      if (!source.includes(required)) throw new Error('OpenClaw pinned read guard is missing: ' + required);
    }
    const readSignature = '\tasync readFile(params) {';
    if (original.split(readSignature).length !== 2) throw new Error('expected one pinned remote read method');
    original = original.replace(readSignature, `\tcanReadWithoutStat(params) {
\t\t// A nested root can have symlink ancestors that open(root, O_NOFOLLOW)
\t\t// alone cannot check. Keep its existing stat/canonical-path validation.
\t\tconst target = this.resolveTarget(params);
\t\treturn path.posix.dirname(target.mountRootPath) === "/" && target.containerPath !== target.mountRootPath;
\t}
` + readSignature);
  }
  if (original.includes('clawbox-batched-stat-v1')) return source.slice(0, start) + original + source.slice(end);
  const pattern = /\tasync stat\(params\) \{[\s\S]*?\n\t\}\n\tgetMounts\(\) \{/g;
  if ([...original.matchAll(pattern)].length !== 1) {
    throw new Error('expected one OpenClaw remote filesystem stat method');
  }
  const replacement = String.raw`	async stat(params) {
		// clawbox-batched-stat-v1: retain the local mount check between RPCs.
		const target = this.resolveTarget(params);
		const canonicalOutput = (await this.runRemoteScript({
			script: [
				"set -eu",
				"target=\"$1\"",
				"if [ ! -e \"$target\" ] && [ ! -L \"$target\" ]; then exit 0; fi",
				"suffix=\"\"",
				"cursor=\"$target\"",
				"while [ ! -e \"$cursor\" ] && [ ! -L \"$cursor\" ]; do",
				"  parent=$(dirname -- \"$cursor\")",
				"  if [ \"$parent\" = \"$cursor\" ]; then break; fi",
				"  base=$(basename -- \"$cursor\")",
				"  suffix=\"/$base$suffix\"",
				"  cursor=\"$parent\"",
				"done",
				"canonical=$(readlink -f -- \"$cursor\")",
				"printf \"%s%s\\n\" \"$canonical\" \"$suffix\""
			].join("\n"),
			args: [target.containerPath],
			signal: params.signal
		})).stdout.toString("utf8").trim();
		if (!canonicalOutput) return null;
		const canonical = normalizeContainerPath(canonicalOutput);
		if (!this.resolveMountByContainerPath(this.getMounts(), canonical)) throw new Error(` +
    '`Sandbox path escapes allowed mounts; cannot stat files: ${target.containerPath}`' + String.raw`);
		const [kindRaw = "", linksRaw = "1", sizeRaw = "0", mtimeRaw = "0"] = (await this.runRemoteScript({
			script: "set -eu\nstat -c \"%F|%h|%s|%y\" -- \"$1\"",
			args: [canonical],
			signal: params.signal
		})).stdout.toString("utf8").trim().split("|");
		if (kindRaw === "regular file" && hasMultipleHardlinks(linksRaw)) throw new Error(` +
    '`Hardlinked path is not allowed under sandbox mount root: ${canonical}`' + String.raw`);
		return {
			type: kindRaw === "directory" ? "directory" : kindRaw === "regular file" ? "file" : "other",
			size: parseSandboxStatSize(sizeRaw),
			mtimeMs: parseSandboxStatMtimeMs(mtimeRaw)
		};
	}
	getMounts() {`;
  return source.slice(0, start) + original.replace(pattern, () => replacement) + source.slice(end);
}

function patchSandboxRead(source) {
  const start = source.indexOf('function createSandboxedReadTool(params) {');
  if (start < 0) return null;
  if (source.includes('clawbox-guarded-read-access-v1')) return source;
  if (source.includes('clawbox-invocation-read-v1')) return patchReadAccess(source);
  const factory = `function createSandboxedReadTool(params) {
\treturn createOpenClawReadTool(createReadTool(params.root, { operations: createSandboxReadOperations(params) }), {
\t\tmodelContextWindowTokens: params.modelContextWindowTokens,
\t\timageSanitization: params.imageSanitization
\t});
}`;
  if (!source.includes(factory)) throw new Error('unexpected OpenClaw sandbox read factory');
  let next = source.replace(factory, `function createSandboxedReadTool(params) {
\t// clawbox-invocation-read-v1: MIME and content use one invocation-local read.
\tconst create = () => createOpenClawReadTool(createReadTool(params.root, { operations: createSandboxReadOperations(params) }), {
\t\tmodelContextWindowTokens: params.modelContextWindowTokens,
\t\timageSanitization: params.imageSanitization
\t});
\treturn { ...create(), execute: (...args) => create().execute(...args) };
}`);
  const opsStart = next.indexOf('function createSandboxReadOperations(params) {');
  const opsEnd = next.indexOf('\nfunction createSandboxWriteOperations(', opsStart);
  if (opsStart < 0 || opsEnd < 0) throw new Error('OpenClaw sandbox read operations are missing');
  let ops = next.slice(opsStart, opsEnd);
  const read = /params\.bridge\.readFile\(\{\s*filePath: absolutePath,\s*cwd: params\.root\s*\}\)/g;
  if ([...ops.matchAll(read)].length !== 2) throw new Error('expected two OpenClaw sandbox content reads');
  ops = ops.replace(read, 'readOnce(absolutePath)');
  ops = ops.replace('function createSandboxReadOperations(params) {', `function createSandboxReadOperations(params) {
\tconst reads = new Map();
\tconst readOnce = (absolutePath) => {
\t\tif (!reads.has(absolutePath)) reads.set(absolutePath, params.bridge.readFile({ filePath: absolutePath, cwd: params.root }));
\t\treturn reads.get(absolutePath);
\t};`);
  return patchReadAccess(next.slice(0, opsStart) + ops + next.slice(opsEnd));
}

function patchReadAccess(source) {
  const start = source.indexOf('function createSandboxReadOperations(params) {');
  const end = source.indexOf('\nfunction createSandboxWriteOperations(', start);
  if (start < 0 || end < 0) throw new Error('OpenClaw sandbox read operations are missing');
  const original = source.slice(start, end);
  const access = 'access: (absolutePath) => assertSandboxFileExists(params, absolutePath),';
  if (original.split(access).length !== 2) throw new Error('expected one sandbox Read access check');
  const updated = original.replace(access, `access: async (absolutePath) => {
\t\t\t// clawbox-guarded-read-access-v1: the pinned read also checks access.
\t\t\tif (params.bridge.canReadWithoutStat?.({ filePath: absolutePath, cwd: params.root })) {
\t\t\t\ttry { await readOnce(absolutePath); return; }
\t\t\t\tcatch (error) {
\t\t\t\t\t// Only a failed filesystem command needs the original access error.
\t\t\t\t\t// Policy rejection, timeout, transport failure and cancellation propagate.
\t\t\t\t\tif (error?.code !== 1) throw error;
\t\t\t\t\t// Preserve the original stat/access error when the read failed.
\t\t\t\t\tawait assertSandboxFileExists(params, absolutePath);
\t\t\t\t\tthrow error;
\t\t\t\t}
\t\t\t}
\t\t\tawait assertSandboxFileExists(params, absolutePath);
\t\t},`);
  return source.slice(0, start) + updated + source.slice(end);
}

function patchOpenClaw(dist) {
if (!fs.existsSync(dist)) throw new Error(`OpenClaw dist directory is missing: ${dist}`);

let rootPatchCount = 0;
let skillsPatchCount = 0;
let noOpEditPatchCount = 0;
let sandboxPreflightPatchCount = 0;
let cliCompactionPatchCount = 0;
let agentCompactionPatchCount = 0;
let toolContextGuardPatchCount = 0;
let promptPrecheckPatchCount = 0;
let remoteStatPatchCount = 0;
let sandboxReadPatchCount = 0;
const changed = [];
for (const name of fs.readdirSync(dist)) {
  if (!name.endsWith(".js")) continue;
  const file = path.join(dist, name);
  const source = fs.readFileSync(file, "utf8");
  let next = source.replace(
    /remoteWorkspaceDir:\s*path\.posix\.join\(runtimeRootDir,\s*"workspace"\)/g,
    "remoteWorkspaceDir: workspaceRoot",
  );
  const batchedStat = patchRemoteStat(next);
  if (batchedStat !== null) {
    next = batchedStat;
    remoteStatPatchCount += 1;
  }
  const sharedRead = patchSandboxRead(next);
  if (sharedRead !== null) {
    next = sharedRead;
    sandboxReadPatchCount += 1;
  }
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
  // The tool-loop context guard and pre-prompt precheck can independently
  // enter overflow recovery even when agent auto-compaction is disabled.
  // Recorded inference must not inject a summary call or truncate history.
  const toolGuardSignature = 'function installToolResultContextGuard(params) {';
  const toolGuard = '\n\tif (process.env.CLAWBOX_REPLAY_NO_AUTO_COMPACTION === "1") return () => {};';
  if (next.includes(toolGuardSignature)) {
    if (!next.includes(toolGuardSignature + toolGuard)) next = next.replace(toolGuardSignature, toolGuardSignature + toolGuard);
    toolContextGuardPatchCount += 1;
  }
  const promptPrecheck = 'const shouldSkipPrecheck = skipPromptSubmission ||';
  const replayPrecheck = 'const shouldSkipPrecheck = process.env.CLAWBOX_REPLAY_NO_AUTO_COMPACTION === "1" || skipPromptSubmission ||';
  if (next.includes(promptPrecheck)) next = next.replace(promptPrecheck, replayPrecheck);
  if (next.includes(replayPrecheck)) promptPrecheckPatchCount += 1;
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
if (remoteStatPatchCount !== 1) {
  throw new Error(`expected one OpenClaw remote filesystem bridge, found ${remoteStatPatchCount}`);
}
if (sandboxReadPatchCount !== 1) {
  throw new Error(`expected one OpenClaw sandbox read factory, found ${sandboxReadPatchCount}`);
}
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
if (toolContextGuardPatchCount !== 1 || promptPrecheckPatchCount !== 1) {
  throw new Error(`expected one tool-loop and prompt context guard, found ${toolContextGuardPatchCount}/${promptPrecheckPatchCount}`);
}
console.log(`OpenClaw SSH backend and no-op edit continuation verified; changed: ${changed.join(", ") || "none"}`);
return changed;
}

module.exports = { patchOpenClaw, patchRemoteStat, patchSandboxRead };
if (require.main === module) {
  const globalRoot = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
  patchOpenClaw(path.join(globalRoot, "openclaw", "dist"));
}
