#!/usr/bin/env python3
"""Generate the paired shared-LLC traces and complete schema-4 experiments.

The generated YAML still runs through ``clawbox experiment``; this script only
materializes deterministic inputs from an already configured host spec.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import shlex
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clawbox.experiments.spec import ExperimentSpec


DEFAULTS = {
    "concurrency": 16,
    "threads": 2,
    "working_set_mib": 2,
    "steps": 300_000_000,
    "heavy_phases": 12,
    "late_short_phases": 8,
    "short_seconds": 0.20,
    "min_duration_seconds": 1.0,
    "group_max_active": 4,
    "pressure_ratio": 0.10,
    "pressure_samples": 2,
    "include_smt": True,
}


def _event_pair(trace_id: str, sequence: int, messages: list[dict[str, Any]],
                output: dict[str, Any]) -> list[dict[str, Any]]:
    common = {
        "schema_version": 6,
        "trace_id": trace_id,
        "span_id": f"model-{sequence}",
        "sequence_no": sequence,
        "kind": "llm",
        "name": "recorded-agent-model",
        "input": {"messages": messages, "requested_args": None},
        "wall_time_ns": str(sequence * 20_000_000),
    }
    return [
        {**common, "record_type": "span_start"},
        {
            **common,
            "record_type": "span_end",
            "duration_ns": "10000000",
            "output": {"content": output},
            "status": {"code": "ok"},
        },
    ]


def _c_source(*, threads: int, working_set_mib: int, steps: int) -> str:
    items = working_set_mib * 1024 * 1024 // 4
    if items < 2 or items & (items - 1):
        raise ValueError("--working-set-mib must produce a power-of-two uint32 array")
    if threads == 2:
        return f"""#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define ITEMS ({items // 1024}u * 1024u)
#define STEPS {steps}u

static uint32_t *links;
static pthread_barrier_t start_barrier;

static void *chase(void *argument) {{
    uintptr_t seed = (uintptr_t)argument;
    uint32_t index = (uint32_t)seed;
    pthread_barrier_wait(&start_barrier);
    for (uint32_t i = 0; i < STEPS; ++i)
        index = links[index];
    return (void *)(uintptr_t)index;
}}

int main(void) {{
    if (posix_memalign((void **)&links, 4096, ITEMS * sizeof(*links)) != 0)
        return 2;
    for (uint32_t i = 0; i < ITEMS; ++i)
        links[i] = (i * 5u + 1u) & (ITEMS - 1u);
    pthread_barrier_init(&start_barrier, NULL, 2);
    pthread_t first, second;
    pthread_create(&first, NULL, chase, (void *)(uintptr_t)0u);
    pthread_create(&second, NULL, chase, (void *)(uintptr_t)1u);
    void *a, *b;
    pthread_join(first, &a);
    pthread_join(second, &b);
    printf("%lu %lu\\n", (unsigned long)a, (unsigned long)b);
    free(links);
    return 0;
}}
"""
    return f"""#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#define ITEMS {items}u
#define STEPS {steps}u
#define THREADS {threads}u
static uint32_t *links;
static pthread_barrier_t start_barrier;
static void *chase(void *argument) {{
    uint32_t index = (uint32_t)(uintptr_t)argument;
    pthread_barrier_wait(&start_barrier);
    for (uint32_t i = 0; i < STEPS; ++i) index = links[index];
    return (void *)(uintptr_t)index;
}}
int main(void) {{
    if (posix_memalign((void **)&links, 4096, ITEMS * sizeof(*links)) != 0) return 2;
    for (uint32_t i = 0; i < ITEMS; ++i) links[i] = (i * 5u + 1u) & (ITEMS - 1u);
    pthread_barrier_init(&start_barrier, NULL, THREADS);
    pthread_t workers[THREADS]; void *results[THREADS];
    for (uintptr_t i = 0; i < THREADS; ++i) pthread_create(&workers[i], NULL, chase, (void *)i);
    for (uint32_t i = 0; i < THREADS; ++i) pthread_join(workers[i], &results[i]);
    for (uint32_t i = 0; i < THREADS; ++i) printf("%lu%c", (unsigned long)results[i], i+1 == THREADS ? '\\n' : ' ');
    free(links); return 0;
}}
"""


def _write_trace(path: Path, *, trace_id: str, source: str, short_prefix: int,
                 heavy_phases: int, short_seconds: float, guest_result: str) -> None:
    prompt = "Run the recorded shared-LLC workload and leave llc-result.txt."
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    encoded = base64.b64encode(source.encode()).decode()
    commands = [
        "python3 -c 'import base64;open(\"/tmp/llc_shared_chase.c\",\"wb\").write("
        f"base64.b64decode(\"{encoded}\"))' && "
        "gcc -O2 -pthread /tmp/llc_shared_chase.c -o /tmp/llc_shared_chase"
    ]
    commands += [f"python3 -c 'import time; time.sleep({short_seconds:.2f})'"
                 for _ in range(short_prefix)]
    commands += ["/tmp/llc_shared_chase >/dev/null"] * heavy_phases
    commands += [f"touch {shlex.quote(guest_result)}"]
    records: list[dict[str, Any]] = []
    for sequence, command in enumerate(commands):
        call_id = f"exec-{sequence}"
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "exec", "arguments": json.dumps({"command": command})},
            }],
        }
        records.extend(_event_pair(trace_id, sequence, copy.deepcopy(messages), assistant))
        messages.extend([assistant, {"role": "tool", "tool_call_id": call_id, "content": "ok"}])
    records.extend(_event_pair(trace_id, len(commands), copy.deepcopy(messages), {
        "role": "assistant", "content": "All shared-LLC phases completed.", "tool_calls": [],
    }))
    payload = "\r\n".join(json.dumps(row, separators=(",", ":")) for row in records) + "\r\n"
    path.write_bytes(payload.encode())


def _saved_repository(raw: dict[str, Any], explicit: str | None,
                      explicit_commit: str | None) -> tuple[str, str]:
    cases = raw.get("workload", {}).get("cases", [])
    repository = explicit or next((case.get("repository") for case in cases
                                   if case.get("repository")), None)
    base_commit = explicit_commit or next((case.get("base_commit") for case in cases
                                           if case.get("base_commit")), None)
    if not repository or not base_commit:
        raise ValueError("base spec has no saved repository/base_commit; pass both options explicitly")
    return repository, base_commit


def _apply_node_cpus(raw: dict[str, Any], values: list[str]) -> None:
    requested = dict(value.split("=", 1) for value in values)
    nodes = raw["environment"]["resources"]["compute_nodes"]
    known = {node["node_id"] for node in nodes}
    unknown = set(requested) - known
    if unknown:
        raise ValueError("unknown compute node(s): " + ", ".join(sorted(unknown)))
    for node in nodes:
        if node["node_id"] in requested:
            node["cpus"] = requested[node["node_id"]]


def _make_spec(base: dict[str, Any], *, experiment_id: str, order: str,
               trace_root: PurePosixPath, repository: str, base_commit: str,
               args: argparse.Namespace) -> dict[str, Any]:
    raw = copy.deepcopy(base)
    raw["experiment_id"] = f"{experiment_id}-{order}"
    _apply_node_cpus(raw, args.node_cpus)
    raw["environment"]["runtime"]["vcpu"] = args.runtime_vcpu
    raw["environment"]["runtime"]["memory_mib"] = args.runtime_memory_mib
    raw["environment"]["sandbox"]["vcpu"] = args.tool_vcpu
    raw["environment"]["sandbox"]["memory_mib"] = args.tool_memory_mib
    raw["environment"]["sandbox"]["preflight_command"] = "command -v gcc >/dev/null"
    early = str(trace_root / "llc-shared-chase-early.jsonl")
    late = str(trace_root / "llc-shared-chase-late.jsonl")
    validation = f"test -f {shlex.quote(args.guest_result)}"
    raw["workload"] = {
        "source": "recorded_trace",
        "input": early,
        "repetitions": 1,
        "cases": [
            {"case_id": "shared-early", "prompt": "Run shared-LLC heavy phases.",
             "source": "recorded_trace", "source_reference": early,
             "repository": repository, "base_commit": base_commit,
             "replay_trace_reference": early, "validation": validation,
             "metadata": {"phase_shape": "heavy-first"}},
            {"case_id": "shared-late", "prompt": "Run short setup, then shared-LLC phases.",
             "source": "recorded_trace", "source_reference": late,
             "repository": repository, "base_commit": base_commit,
             "replay_trace_reference": late, "validation": validation,
             "metadata": {"phase_shape": "short-prefix-then-heavy"}},
        ],
        "session_assignment": "round_robin",
    }
    raw.setdefault("measurement", {}).setdefault("validation", {})["command"] = validation
    placement = raw["mechanisms"]["placement"]
    placement.update({
        "policy": "explicit",
        "tool_call_cpu_policy": "c2",
        "tool_call_cpu_pressure_ratio": args.pressure_ratio,
        "tool_call_cpu_pressure_samples": args.pressure_samples,
        "tool_call_cpu_group_max_active": args.group_max_active,
        "tool_call_cpu_min_duration_seconds": args.min_duration_seconds,
        "tool_call_cpu_include_smt": args.include_smt,
        "session_compute_nodes": [args.session_nodes[i % len(args.session_nodes)]
                                  for i in range(args.concurrency)],
    })
    arms = {
        "c0": {"name": "c0-shared", "override": {"placement.tool_call_cpu_policy": "c0"}},
        "c2": {"name": "c2-shared-smt", "override": {}},
    }
    arm_order = ("c2", "c0") if order == "c2-first" else ("c0", "c2")
    raw["comparison"] = {"recipe": "custom", "arms": [arms[name] for name in arm_order]}
    execution = raw["execution"]
    execution.update({
        "concurrency_levels": [args.concurrency],
        "randomized_order": False,
        "arrival_schedule": "burst",
        "stagger_interval_seconds": 0.0,
        "arm_timeout_seconds": args.arm_timeout_seconds,
        "command_timeout_seconds": args.command_timeout_seconds,
    })
    return raw


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-spec", type=Path, required=True,
                        help="saved schema-4 spec containing host templates, resources, and evidence")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--trace-runtime-dir",
                        help="absolute POSIX path to output-dir as seen by the experiment host")
    parser.add_argument("--experiment-id", default="llc-shared-c16-b32")
    parser.add_argument("--repository")
    parser.add_argument("--base-commit")
    parser.add_argument("--concurrency", type=int, default=DEFAULTS["concurrency"])
    parser.add_argument("--session-node", dest="session_nodes", action="append",
                        default=[], help="compute node rotation; repeat, e.g. node0 then node1")
    parser.add_argument("--node-cpus", action="append", default=[],
                        metavar="NODE=CPULIST", help="override a saved compute-node CPU pool")
    parser.add_argument("--runtime-vcpu", type=int, default=2)
    parser.add_argument("--runtime-memory-mib", type=int, default=1024)
    parser.add_argument("--tool-vcpu", type=int, default=2)
    parser.add_argument("--tool-memory-mib", type=int, default=3072)
    parser.add_argument("--threads", type=int, default=DEFAULTS["threads"])
    parser.add_argument("--working-set-mib", type=int, default=DEFAULTS["working_set_mib"])
    parser.add_argument("--steps", type=int, default=DEFAULTS["steps"])
    parser.add_argument("--heavy-phases", type=int, default=DEFAULTS["heavy_phases"])
    parser.add_argument("--late-short-phases", type=int, default=DEFAULTS["late_short_phases"])
    parser.add_argument("--short-seconds", type=float, default=DEFAULTS["short_seconds"])
    parser.add_argument("--min-duration-seconds", type=float,
                        default=DEFAULTS["min_duration_seconds"])
    parser.add_argument("--group-max-active", type=int, default=DEFAULTS["group_max_active"])
    parser.add_argument("--pressure-ratio", type=float, default=DEFAULTS["pressure_ratio"])
    parser.add_argument("--pressure-samples", type=int, default=DEFAULTS["pressure_samples"])
    parser.add_argument("--include-smt", action=argparse.BooleanOptionalAction,
                        default=DEFAULTS["include_smt"])
    parser.add_argument("--guest-result", default="/workspace/llc-result.txt")
    parser.add_argument("--arm-timeout-seconds", type=int, default=1200)
    parser.add_argument("--command-timeout-seconds", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.concurrency < 1 or args.threads < 1 or args.steps < 1 or args.heavy_phases < 1:
        raise SystemExit("concurrency, threads, steps, and heavy phases must be positive")
    base = yaml.safe_load(args.base_spec.read_text())
    repository, base_commit = _saved_repository(base, args.repository, args.base_commit)
    if not args.session_nodes:
        saved = base.get("mechanisms", {}).get("placement", {}).get("session_compute_nodes", [])
        args.session_nodes = list(dict.fromkeys(saved))
    if not args.session_nodes:
        args.session_nodes = [node["node_id"] for node in
                              base["environment"]["resources"]["compute_nodes"]]
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    trace_root = PurePosixPath(args.trace_runtime_dir or output.as_posix())
    if not trace_root.is_absolute():
        raise SystemExit("--trace-runtime-dir must be an absolute POSIX path")
    source = _c_source(threads=args.threads, working_set_mib=args.working_set_mib,
                       steps=args.steps)
    _write_trace(output / "llc-shared-chase-early.jsonl", trace_id="llc-shared-chase-early",
                 source=source, short_prefix=0, heavy_phases=args.heavy_phases,
                 short_seconds=args.short_seconds, guest_result=args.guest_result)
    _write_trace(output / "llc-shared-chase-late.jsonl", trace_id="llc-shared-chase-late",
                 source=source, short_prefix=args.late_short_phases,
                 heavy_phases=args.heavy_phases, short_seconds=args.short_seconds,
                 guest_result=args.guest_result)
    specs = []
    for order in ("c2-first", "c0-first"):
        raw = _make_spec(base, experiment_id=args.experiment_id, order=order,
                         trace_root=trace_root, repository=repository,
                         base_commit=base_commit, args=args)
        ExperimentSpec.model_validate(raw)
        path = output / f"experiment-{order}.yaml"
        path.write_text(yaml.safe_dump(raw, sort_keys=False))
        specs.append(path)
    manifest = {
        "schema_version": 1,
        "generator": "scripts/prepare_llc_shared_experiment.py",
        "base_spec": str(args.base_spec.resolve()),
        "base_spec_sha256": hashlib.sha256(args.base_spec.read_bytes()).hexdigest(),
        "trace_runtime_dir": str(trace_root),
        "parameters": {key: getattr(args, key) for key in (
            "concurrency", "runtime_vcpu", "runtime_memory_mib", "tool_vcpu",
            "tool_memory_mib", "threads", "working_set_mib", "steps",
            "heavy_phases", "late_short_phases", "short_seconds",
            "min_duration_seconds", "group_max_active", "pressure_ratio",
            "pressure_samples", "include_smt", "session_nodes", "node_cpus",
            "arm_timeout_seconds", "command_timeout_seconds",
        )},
        "files": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in [*specs, output / "llc-shared-chase-early.jsonl",
                               output / "llc-shared-chase-late.jsonl"]},
        "commands": [
            f"python -m clawbox.cli experiment validate {shlex.quote(str(path))} --inputs"
            for path in specs
        ] + [
            f"python -m clawbox.cli experiment qualify {shlex.quote(str(path))} --concurrency {args.concurrency}"
            for path in specs
        ] + [
            f"python -m clawbox.cli experiment run {shlex.quote(str(path))} "
            f"--run-id {shlex.quote(args.experiment_id + '-' + order)}"
            for path, order in zip(specs, ("c2-first", "c0-first"))
        ],
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output_dir": str(output), "manifest": str(manifest_path),
                      "specs": [str(path) for path in specs]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
