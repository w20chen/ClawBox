import assert from "node:assert/strict";
import test from "node:test";
import {buildSandboxExecEnvelope, instrumentExecParams} from "../dist/exec-instrumentation.js";
import {SessionObservations} from "../dist/session-observations.js";
import {eventSignal} from "../dist/signals.js";
import {SidecarClient} from "../dist/client.js";
import {register} from "node:module";

test("awaited agent-start hook registers child ownership before returning", async () => {
  register(`data:text/javascript,${encodeURIComponent(`
    export async function resolve(specifier, context, nextResolve) {
      if (specifier === 'openclaw/plugin-sdk/plugin-entry')
        return {url: 'data:text/javascript,export const definePluginEntry = entry => entry', shortCircuit: true};
      return nextResolve(specifier, context);
    }
  `)}`, import.meta.url);
  const {default: plugin} = await import("../dist/index.js");
  const hooks = new Map();
  const previous = globalThis.fetch;
  let acknowledge, entered, finished = false;
  const pending = new Promise(resolve => { entered = resolve; });
  globalThis.fetch = async (url, options) => {
    assert.ok(url.endsWith('/v1/clawbox/session-register'));
    assert.equal(JSON.parse(options.body).session_key, 'child');
    entered();
    await new Promise(resolve => { acknowledge = resolve; });
    return new Response('{"registered":true}', {status: 200});
  };
  try {
    plugin.register({pluginConfig: {autoStartSidecar: false, requireHostPrediction: true,
      decisionTimeoutMs: 1000, trace: {trace_dir: ""}},
      on(name, callback) { hooks.set(name, callback); }, logger: {info() {}, warn() {}, error() {}}});
    const gate = hooks.get('before_agent_start')({}, {sessionId: 'child-id', sessionKey: 'child'})
      .then(() => { finished = true; });
    await pending;
    assert.equal(finished, false);
    acknowledge();
    await gate;
    assert.equal(finished, true);
  } finally { globalThis.fetch = previous; }
});

test("host session registration is acknowledged before caching and isolates children", async () => {
  const previous = globalThis.fetch;
  const keys = [];
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "http://sidecar/v1/clawbox/session-register");
    keys.push(JSON.parse(options.body).session_key);
    return new Response('{"registered":true}', {status: 200});
  };
  try {
    const client = new SidecarClient({endpoint: "http://sidecar", requireHostPrediction: true, decisionTimeoutMs: 500});
    await client.registerLogicalSession("parent");
    await client.registerLogicalSession("parent");
    await client.registerLogicalSession("child-one");
    await client.registerLogicalSession("child-two");
    assert.deepEqual(keys, ["parent", "child-one", "child-two"]);
  } finally { globalThis.fetch = previous; }
});

test("host envelope completion skips local lookups but preserves completion and execution identity", async () => {
  const {default: plugin} = await import("../dist/index.js");
  const previous = globalThis.fetch;
  try {
    for (const backend of ["hook-only", "managed-wrapper", "marker"]) {
      const hooks = new Map(), requests = [], warnings = [];
      let registeredId, reported;
      globalThis.fetch = async (url, options) => {
        const path = new URL(url).pathname;
        requests.push(path);
        let body = {};
        if (path === '/v1/decisions/tool') {
          const load = {targets: Object.fromEntries([
            'duration_ms', 'cpu_time_seconds', 'cpu_avg_cores', 'cpu_peak_cores',
            'sampled_peak_rss_bytes', 'memory_total_peak_bytes', 'memory_extra_peak_bytes',
          ].map(name => [name, {status: 'unavailable', backend: 'runtime', unit: 'ms',
            evidence_counts: [], buckets: {edges: [], probabilities: null}, context: [], assumptions: []}]))};
          body = {decision_id: 'decision', lease_id: 'lease', action: 'allow',
            prediction: {tool: {load}, trie: {load}, lattice: {load}}};
        } else if (path === '/v2/executions') {
          registeredId = JSON.parse(options.body).execution_id;
          body = {one_time_token: 'test-token'};
        } else if (path.endsWith('/scope')) {
          body = {execution_scope: {execution_id: registeredId, pid: 42, root_pid: 42,
            source: 'clawtune-sidecar-host-cgroup', attribution_source: 'trusted-execution-root-pid'}};
        } else if (path.endsWith('/telemetry')) {
          body = {tool_resource: null};
        } else if (path === '/v1/signals') {
          const signal = JSON.parse(options.body);
          if (signal.type === 'tool.completed') reported = signal;
        } else assert.fail(`unexpected request ${path}`);
        return new Response(JSON.stringify(body), {status: 200});
      };
      plugin.register({pluginConfig: {autoStartSidecar: false, executionBackend: backend,
        sandboxExecEnvelope: backend === 'hook-only', trace: {trace_dir: ''}},
        on(name, callback) { hooks.set(name, callback); },
        logger: {info() {}, warn(message) { warnings.push(message); }, error() {}}});
      const event = {toolName: 'exec', toolCallId: 'call', params: {command: 'true'}};
      const context = {sessionId: 'session', runId: 'run'};
      const before = await hooks.get('before_tool_call')(event, context);
      const executionId = before.params.env.CLAWTUNE_EXECUTION_ID;
      await hooks.get('after_tool_call')({...event, durationMs: 5, result: {details: {exitCode: 0}}}, context);
      assert.deepEqual(warnings, []);
      assert.equal(reported.identity.execution_id, executionId);
      assert.equal(reported.payload.execution_id, executionId);
      assert.equal(reported.payload.succeeded, true);
      assert.equal(reported.payload.decision_id, 'decision');
      const lookups = requests.filter(path => path.endsWith('/scope') || path.endsWith('/telemetry'));
      assert.deepEqual(lookups, backend === 'hook-only' ? [] : [
        `/v2/executions/${executionId}/scope`, `/v2/executions/${executionId}/telemetry`,
      ]);
      if (backend !== 'hook-only') {
        assert.equal(reported.payload.resource_scope.root_pid, 42);
        assert.equal(reported.payload.resource_scope.execution_id, executionId);
      }
    }
  } finally { globalThis.fetch = previous; }
});

test("parent and multiple child sessions with identical tool IDs remain independent", async () => {
  const sent = [];
  const owner = name => ({gatewayId: "g", runtimeId: "shared", sessionId: name,
    sessionKey: name + "-key", runId: "run", agentId: "same-agent"});
  const observer = new SessionObservations((event, context) => owner(context.session), async value => sent.push(value));
  try {
    observer.model({callId: "m"}, {session: "parent"}, true);
    for (const session of ["one", "two"]) {
      observer.spawned({childSessionKey: session + "-key"}, {requesterSessionKey: "parent-key"});
      observer.model({callId: "m"}, {session}, true);
      observer.transcript({sessionKey: session + "-key", messageId: "same-message", message: {
        role: "assistant", content: [{type: "toolCall", id: "same-tool", name: "exec"}]}});
    }
    observer.afterTool({toolCallId: "same-tool"}, {session: "one"});
    await new Promise(resolve => setImmediate(resolve));
    const latest = Object.fromEntries(sent.map(row => [row.identity.session_id, row.payload]));
    assert.equal(latest.one.parent_session_key, "parent-key");
    assert.equal(latest.two.parent_session_key, "parent-key");
    assert.equal(latest.one.batch.tools[0].settled, true);
    assert.equal(latest.two.batch.tools[0].settled, false);
    assert.equal(latest.parent.model_call_id, "m");
  } finally { await observer.close(); }
});

test("yielded parent identifies active children and excludes no-completion work", async () => {
  const sent = [];
  const owner = {gatewayId: "g", runtimeId: "shared", sessionId: "parent",
    sessionKey: "parent-key", runId: "parent-run", agentId: "agent"};
  const observer = new SessionObservations(() => owner, async value => sent.push(value));
  try {
    observer.model({callId: "model"}, {}, true);
    for (const [child, completion] of [["critical", true], ["background", false]]) {
      observer.spawned({childSessionKey: child, runId: child + "-run"},
        {requesterSessionKey: "parent-key"});
      observer.afterTool({toolName: "sessions_spawn", toolCallId: "spawn-" + child,
        params: {mode: "run", expectsCompletionMessage: completion}, result: {details: {
          status: "accepted", childSessionKey: child, runId: child + "-run"}}}, {});
    }
    observer.afterTool({toolName: "sessions_yield", toolCallId: "yield", params: {},
      result: {details: {status: "yielded"}}}, {});
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(sent.at(-1).payload.active_child_session_keys, ["background", "critical"]);
    assert.deepEqual(sent.at(-1).payload.wait, {reason: "subagent_completion", mode: "any",
      session_keys: ["critical"]});
  } finally { await observer.close(); }
});

test("Cube envelope selects the nested model and carries only the control receipt", () => {
  const load = backend => ({schema_version: "call_load.v2", scope: "tool_call",
    memory_measurement: "guest_memtotal_minus_memavailable", targets: {duration_ms: {backend}}});
  const decision = {prediction: {tool: {load: load("runtime")}, lattice: {load: load("lattice")}},
    placement: {host_kb: {prediction_receipt: "opaque-host-receipt", tool_name: "terminal_exec"}}};
  for (const [model, backend] of [["tool", "runtime"], ["lattice", "lattice"]]) {
    const envelope = buildSandboxExecEnvelope("echo hello", "execution", decision, model, {
      session_id: "logical-session", session_key: "agent:main:subagent:child",
      agent_id: "agent", tool_call_id: "tool-call", run_id: "run",
    });
    const header = JSON.parse(Buffer.from(envelope.split("\n")[0].split("b64:")[1], "base64url"));
    assert.equal(header.call_prediction.targets.duration_ms.backend, backend);
    assert.equal(header.prediction_receipt, "opaque-host-receipt");
    assert.equal(header.tool_name, "terminal_exec");
    assert.equal(header.logical_session_id, "logical-session");
    assert.equal(header.session_key, "agent:main:subagent:child");
    assert.equal(header.agent_id, "agent");
    assert.equal(header.tool_call_id, "tool-call");
    assert.equal(header.run_id, "run");
    assert.equal(Buffer.from(header.profile_command_b64, "base64url").toString(), "echo hello");
  }
});

test("required host prediction fails before hook-only execution without a receipt", async () => {
  await assert.rejects(instrumentExecParams(
    {toolName: "exec", params: {command: "true"}}, {}, {}, null, {},
    {executionBackend: "hook-only", sandboxExecEnvelope: true, instrumentTools: ["exec"], instrumentHosts: ["*"],
      requireHostPrediction: true}), /Host prediction receipt/);
});

test("hook-only instrumentation forwards logical Tool identity into the Cube envelope", async () => {
  const result = await instrumentExecParams(
    {toolName: "exec", params: {command: "true"}}, {}, {
      session_id: "logical-session", session_key: "agent:main:child", agent_id: "agent",
      tool_call_id: "tool-call", run_id: "run",
    }, null, {}, {executionBackend: "hook-only", sandboxExecEnvelope: true,
      instrumentTools: ["exec"], instrumentHosts: ["*"], requireHostPrediction: false});
  const header = JSON.parse(Buffer.from(
    result.params.command.split("\n")[0].split("b64:")[1], "base64url"));
  assert.equal(header.logical_session_id, "logical-session");
  assert.equal(header.session_key, "agent:main:child");
  assert.equal(header.tool_call_id, "tool-call");
});

test("delivered session observer previews full batches and settles logical returns", async () => {
  const owner = {gatewayId: "g", runtimeId: "r", sessionId: "s", sessionKey: "key", runId: "run", agentId: "a"};
  const sent = [], previewed = [];
  let transcript;
  const observer = new SessionObservations(() => owner, async value => sent.push(value), {
    onSessionTranscriptUpdate(handler) { transcript = handler; return () => {}; },
  }, true, async call => { previewed.push(call.id); return {durationMs: 100, source: "lattice"}; });
  try {
    observer.model({callId: "model"}, {}, true);
    transcript({sessionKey: "key", messageId: "assistant", message: {role: "assistant", content: [
      {type: "toolCall", id: "one", name: "exec"}, {type: "toolCall", id: "two", name: "exec"},
    ]}});
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(previewed, ["one", "two"]);
    observer.beforeTool({toolName: "exec", toolCallId: "one"}, {});
    observer.afterTool({toolName: "exec", toolCallId: "one", result: {}}, {});
    await new Promise(resolve => setImmediate(resolve));
    const batch = sent.at(-1).payload.batch;
    assert.equal(batch.tools[0].settled, true);
    assert.equal(batch.tools[1].settled, false);
    assert.equal(batch.tools[1].prediction_source, "lattice");
  } finally { await observer.close(); }
});

test("completion observations use the unified protocol and retain decision identity", () => {
  const signal = eventSignal({event_id: "event", decision_id: "decision", tool_call_id: "tool",
    occurred_at: "2026-11-21T00:00:00Z"});
  assert.equal(signal.schema_version, "clawtune.signal.v1");
  assert.equal(signal.type, "tool.completed");
  assert.equal(signal.identity.prediction_id, "decision");
});
