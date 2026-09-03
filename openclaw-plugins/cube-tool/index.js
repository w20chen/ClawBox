import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";

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
        if (!base || !token) throw new Error("Cube tool bridge is not configured");
        const response = await fetch(`${base}/execute`, {
          method: "POST",
          headers: {"content-type": "application/json", "authorization": `Bearer ${token}`},
          body: JSON.stringify({command, timeout_seconds}),
          signal: context.signal
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || `Cube tool bridge HTTP ${response.status}`);
        return body;
      }
    })
  ]
});
