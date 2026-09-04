import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";

let runtimePredictionIndex;
function runtimePrediction(command) {
  const path = process.env.CLAWBOX_RUNTIME_PREDICTION_FILE;
  if (!path) return null;
  if (runtimePredictionIndex === undefined) {
    try {
      runtimePredictionIndex = JSON.parse(readFileSync(path, "utf8"));
    } catch (_error) {
      runtimePredictionIndex = null;
    }
  }
  if (!runtimePredictionIndex || typeof runtimePredictionIndex !== "object") return null;
  const digest = createHash("sha256").update(command).digest("hex");
  return runtimePredictionIndex[digest] || null;
}

export default defineToolPlugin({
  id: "clawbox-cube-tool",
  name: "ClawBox Cube Tool",
  description: "Routes coding-agent commands to the owning CubeSandbox",
  tools: (tool) => [
    tool({
      name: "cube_shell",
      label: "CubeSandbox shell",
      description: "Run one shell command in the isolated /workspace CubeSandbox. Use this for every file, repository, build, and test operation.",
      parameters: Type.Object({
        command: Type.String({minLength: 1, description: "POSIX shell command to run in /workspace"}),
        timeout_seconds: Type.Optional(Type.Integer({minimum: 1, maximum: 3600}))
      }),
      async execute({command, timeout_seconds}, _config, context) {
        context.signal?.throwIfAborted();
        const base = process.env.CLAWBOX_CUBE_TOOL_URL;
        const token = process.env.CLAWBOX_CUBE_TOOL_TOKEN;
        const session_id = process.env.CLAWBOX_CUBE_TOOL_SESSION_ID;
        if (!base || !token || !session_id) throw new Error("Cube tool bridge is not configured");
        // OpenClaw's tool-call ID is also the native ClawTune span identity.
        // Preserve it across Runtime -> Worker -> Tool; UUID is a fallback for
        // OpenClaw versions that omit an ID from the tool context.
        const execution_id = context.toolCallId || context.tool_call_id || crypto.randomUUID();
        const response = await fetch(`${base}/execute`, {
          method: "POST",
          headers: {"content-type": "application/json", "authorization": `Bearer ${token}`},
          body: JSON.stringify({command, timeout_seconds, execution_id, session_id,
            prediction: runtimePrediction(command)}),
          signal: context.signal
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || `Cube tool bridge HTTP ${response.status}`);
        return body;
      }
    })
  ]
});
