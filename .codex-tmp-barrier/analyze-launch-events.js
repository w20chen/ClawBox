const fs = require("fs");

const path = process.argv[2];
if (!path) throw new Error("usage: node analyze-launch-events.js EVENTS.jsonl");

const events = fs.readFileSync(path, "utf8").trim().split(/\r?\n/).map(JSON.parse);
const selected = (name) => events.filter((event) => event.event === name);
const q = (values, fraction) => {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor(fraction * sorted.length))];
};
const stats = (values) => ({
  count: values.length,
  min: q(values, 0),
  p50: q(values, 0.5),
  p90: q(values, 0.9),
  p95: q(values, 0.95),
  p99: q(values, 0.99),
  max: q(values, 1),
  mean: values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null,
});

const waits = selected("sandbox_create_gate_wait_started");
const acquired = selected("sandbox_create_gate_acquired");
const released = selected("sandbox_create_gate_released");
const created = selected("sandbox_created");
const t0 = Math.min(...waits.map((event) => Number(event.wall_time_ns) / 1e9));
const createdTimes = created.map((event) => Number(event.wall_time_ns) / 1e9);
const maxElapsed = Math.ceil((Math.max(...createdTimes) - t0) / 30) * 30;
const buckets = [];
for (let start = 0; start < maxElapsed; start += 30) {
  const count = createdTimes.filter((time) => time - t0 >= start && time - t0 < start + 30).length;
  buckets.push({ elapsed_s: `${start}-${start + 30}`, vm_created: count, vm_per_s: count / 30 });
}

const roleCounts = {};
for (const event of created) roleCounts[event.role] = (roleCounts[event.role] || 0) + 1;
const serviceByWindow = {};
for (const [label, min, max] of [["0-30", 0, 30], ["30-60", 30, 60], ["60-120", 60, 120], ["120-180", 120, 180], ["180-end", 180, Infinity]]) {
  serviceByWindow[label] = stats(created
    .filter((event) => Number(event.wall_time_ns) / 1e9 - t0 >= min && Number(event.wall_time_ns) / 1e9 - t0 < max)
    .map((event) => event.service_seconds));
}

console.log(JSON.stringify({
  event_count: events.length,
  first_offer_unix_s: t0,
  first_created_after_s: Math.min(...createdTimes) - t0,
  last_created_after_s: Math.max(...createdTimes) - t0,
  created_count: created.length,
  completed_pair_gates: released.length,
  acquired_pair_gates: acquired.length,
  offered_sessions: waits.length,
  role_counts: roleCounts,
  vm_create_service_seconds: stats(created.map((event) => event.service_seconds)),
  pair_gate_held_seconds: stats(released.map((event) => event.held_seconds)),
  acquired_wait_seconds: stats(acquired.map((event) => event.wait_seconds)),
  service_by_elapsed_window: serviceByWindow,
  throughput_30s: buckets,
}, null, 2));
