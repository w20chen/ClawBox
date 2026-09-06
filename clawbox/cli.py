"""Standalone CubeSandbox experiment CLI."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from clawbox.experiments import BASELINES, expand_matrix, load_experiment, spec_digest


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def emit_overview(value: dict[str, Any]) -> None:
    """Render the pre-run resource shape for a human at the terminal."""
    runtime = value["runtime"]
    tool = value["tool"]
    print(f"Experiment: {value['experiment_id']}")
    print(f"Driver/model: {value['agent_driver']} / {value['inference_backend']}")
    print(
        "Per Agent: "
        f"Runtime={runtime['vcpu']} vCPU/{runtime['memory_gib']:g} GiB, "
        f"Tool={tool['vcpu']} vCPU/{tool['memory_gib']:g} GiB, "
        f"pair={value['pair_memory_gib']:g} GiB"
    )
    print("Concurrency:")
    for row in value["concurrency"]:
        print(
            f"  c{row['agents']}: {row['vm_count']} VMs, "
            f"{row['offered_vcpu']} offered vCPU, "
            f"{row['offered_pair_memory_gib']:g} GiB offered / "
            f"{row['pool_memory_gib']:g} GiB pool = "
            f"{row['offered_to_pool_ratio']:.3f}x"
            + (" (overcommit)" if row["memory_overcommit"] else "")
        )
    execution = value["execution"]
    safety = value["safety"]
    print(
        f"Schedule: {execution['arrival_schedule']} "
        f"(stagger={execution['stagger_interval_seconds']:g}s), "
        f"seed={execution['random_seed']}"
    )
    print(
        f"Safety: host free floor={safety['emergency_free_memory_gib']:g} GiB, "
        f"checkpoint/restore headroom={safety['checkpoint_restore_headroom_gib']:g} GiB"
    )
    print(f"Policies ({len(value['policies'])}), arms ({value['arm_count']}):")
    for policy in value["policies"]:
        print(
            f"  {policy['name']}: {policy['admission']} + "
            f"{policy['reclamation']}/{policy['eviction']}/{policy['restore']}"
        )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--output-root", type=Path,
                      default=Path(os.getenv("CLAWBOX_OUTPUT_ROOT", "results")))
    commands = root.add_subparsers(dest="group", required=True)
    experiment = commands.add_parser("experiment")
    sub = experiment.add_subparsers(dest="command", required=True)
    for name in ("validate", "plan", "run"):
        command = sub.add_parser(name)
        command.add_argument("spec", type=Path)
        if name == "run":
            command.add_argument("--run-id")
            command.add_argument("--attempt-id")
            command.add_argument("--owner-id")
    for name in ("status", "collect"):
        command = sub.add_parser(name)
        command.add_argument("run_id")
    describe = sub.add_parser(
        "describe", help="show VM totals, memory overcommit, policies, and arm count",
    )
    describe.add_argument("spec", type=Path)
    describe.add_argument("--json", action="store_true", dest="as_json")
    baselines = sub.add_parser(
        "baselines", help="list complete baseline tuples accepted by configure",
    )
    baselines.add_argument("--all", action="store_true", help="include compatibility aliases")
    baselines.add_argument("--json", action="store_true", dest="as_json")
    configure = sub.add_parser(
        "configure", help="create a validated experiment YAML from a checked-in example",
    )
    configure.add_argument("base", type=Path, help="complete schema-v2 YAML to use as a base")
    configure.add_argument("output", type=Path, help="new experiment YAML")
    configure.add_argument("--force", action="store_true", help="replace the output file")
    configure.add_argument("--experiment-id")
    configure.add_argument("--trace")
    configure.add_argument("--case-id")
    configure.add_argument("--prompt")
    configure.add_argument("--validation-command")
    configure.add_argument("--repetitions", type=int)
    configure.add_argument(
        "--session-assignment", choices=("single_case", "round_robin"),
    )
    configure.add_argument("--concurrency", help="comma-separated offered levels, e.g. 1,5,60")
    configure.add_argument(
        "--baseline", action="append", dest="baselines",
        choices=tuple(
            name for name, value in BASELINES.items()
            if value.implementation_status == "implemented"
        ),
        help="canonical baseline; repeat to build a comparison matrix",
    )
    configure.add_argument("--runtime-template-id")
    configure.add_argument("--tool-template-id")
    configure.add_argument("--runtime-image-reference")
    configure.add_argument("--tool-image-reference")
    configure.add_argument("--runtime-image-digest")
    configure.add_argument("--tool-image-digest")
    configure.add_argument("--runtime-vcpu", type=int)
    configure.add_argument("--tool-vcpu", type=int)
    configure.add_argument("--runtime-memory-gib", type=float)
    configure.add_argument("--tool-memory-gib", type=float)
    configure.add_argument("--target-node")
    configure.add_argument("--pool-memory-gib", type=float)
    configure.add_argument("--emergency-free-memory-gib", type=float)
    configure.add_argument("--checkpoint-headroom-gib", type=float)
    configure.add_argument("--static-tool-memory-mib", type=int)
    configure.add_argument("--full-tool-memory-mib", type=int)
    configure.add_argument("--p90-kb")
    configure.add_argument("--oracle-measurements")
    configure.add_argument("--arrival-schedule", choices=("burst", "fixed_stagger"))
    configure.add_argument("--stagger-seconds", type=float)
    configure.add_argument("--random-seed", type=int)
    configure.add_argument("--arm-timeout-seconds", type=int)
    configure.add_argument("--command-timeout-seconds", type=int)
    configure.add_argument("--memory-sample-interval-seconds", type=float)
    configure.add_argument("--stabilization-seconds", type=float)
    configure.add_argument("--time-scale", type=float)
    configure.add_argument("--openclaw-exec-yield-ms", type=int)
    configure.add_argument("--model-wait-prediction-seconds", type=float)
    configure.add_argument("--model-wait-prediction-source")
    configure.add_argument("--fixed-delay-seconds", type=float)
    configure.add_argument("--prefetch-lead-seconds", type=float)
    configure.add_argument("--inference-backend", choices=("replay", "api"))
    configure.add_argument("--model")
    configure.add_argument("--base-url")
    configure.add_argument("--api-key-env")
    return root


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    try:
        if args.command == "baselines":
            admission_required = {
                "tool_full": ["resources.full_tool_memory_mib"],
                "tool_static": ["resources.static_tool_memory_mib"],
                "tool_p90": ["resources.p90_predictions"],
                "tool_oracle": ["resources.oracle_measurements"],
            }
            rows = []
            for name, value in BASELINES.items():
                if not args.all and value.implementation_status != "implemented":
                    continue
                required_settings = list(
                    admission_required.get(value.admission_policy.value, [])
                )
                if value.eviction_policy.value == "wait_aware_pressure":
                    required_settings.extend([
                        "inference.configuration.model_wait_prediction_seconds",
                        "inference.configuration.model_wait_prediction_source",
                    ])
                rows.append({
                    "name": name,
                    "admission": value.admission_policy.value,
                    "reclamation": value.reclamation_policy.value,
                    "eviction": value.eviction_policy.value,
                    "restore": value.restore_policy.value,
                    "required_settings": required_settings,
                    "status": value.implementation_status,
                })
            if args.as_json:
                emit(rows)
            else:
                print("Available baselines:")
                for row in rows:
                    requirement = (
                        f", requires {', '.join(row['required_settings'])}"
                        if row["required_settings"] else ""
                    )
                    print(
                        f"  {row['name']}: {row['admission']} + "
                        f"{row['reclamation']}/{row['eviction']}/{row['restore']}"
                        f"{requirement}"
                    )
            return 0
        if args.command == "configure":
            from clawbox.experiments.configure import (
                configure_experiment, dump_experiment, experiment_overview,
            )

            if args.output.exists() and not args.force:
                raise ValueError(
                    f"output already exists: {args.output}; pass --force to replace it"
                )
            spec = configure_experiment(
                args.base,
                experiment_id=args.experiment_id,
                trace=args.trace,
                case_id=args.case_id,
                prompt=args.prompt,
                validation_command=args.validation_command,
                repetitions=args.repetitions,
                session_assignment=args.session_assignment,
                concurrency=args.concurrency,
                baseline_names=args.baselines or (),
                runtime_template_id=args.runtime_template_id,
                tool_template_id=args.tool_template_id,
                runtime_image_reference=args.runtime_image_reference,
                tool_image_reference=args.tool_image_reference,
                runtime_image_digest=args.runtime_image_digest,
                tool_image_digest=args.tool_image_digest,
                runtime_vcpu=args.runtime_vcpu,
                tool_vcpu=args.tool_vcpu,
                runtime_memory_gib=args.runtime_memory_gib,
                tool_memory_gib=args.tool_memory_gib,
                target_node=args.target_node or os.getenv("CUBE_NODE"),
                pool_memory_gib=args.pool_memory_gib,
                emergency_free_memory_gib=args.emergency_free_memory_gib,
                checkpoint_headroom_gib=args.checkpoint_headroom_gib,
                static_tool_memory_mib=args.static_tool_memory_mib,
                full_tool_memory_mib=args.full_tool_memory_mib,
                p90_kb=args.p90_kb,
                oracle_measurements=args.oracle_measurements,
                arrival_schedule=args.arrival_schedule,
                stagger_seconds=args.stagger_seconds,
                random_seed=args.random_seed,
                arm_timeout_seconds=args.arm_timeout_seconds,
                command_timeout_seconds=args.command_timeout_seconds,
                memory_sample_interval_seconds=args.memory_sample_interval_seconds,
                stabilization_seconds=args.stabilization_seconds,
                time_scale=args.time_scale,
                openclaw_exec_yield_ms=args.openclaw_exec_yield_ms,
                model_wait_prediction_seconds=args.model_wait_prediction_seconds,
                model_wait_prediction_source=args.model_wait_prediction_source,
                fixed_delay_seconds=args.fixed_delay_seconds,
                prefetch_lead_seconds=args.prefetch_lead_seconds,
                inference_backend=args.inference_backend,
                model=args.model,
                base_url=args.base_url,
                api_key_env=args.api_key_env,
            )
            overview = experiment_overview(spec)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(dump_experiment(spec), encoding="utf-8")
            emit_overview(overview)
            print(f"Wrote: {args.output}")
            print(f"Next: clawbox experiment validate {args.output}")
            print(f"Run:  clawbox --output-root <result-directory> experiment run {args.output}")
            return 0
        if args.command == "describe":
            from clawbox.experiments.configure import experiment_overview

            value = experiment_overview(load_experiment(args.spec))
            emit(value) if args.as_json else emit_overview(value)
            return 0
        if args.command in {"validate", "plan"}:
            spec = load_experiment(args.spec)
            arms = expand_matrix(spec)
            result: dict[str, Any] = {
                "valid": True, "specDigest": spec_digest(spec), "armCount": len(arms),
            }
            if args.command == "plan":
                result["arms"] = [arm.model_dump(mode="json") for arm in arms]
            emit(result)
            return 0
        if args.command == "run":
            from clawbox.experiments.worker import ExperimentWorker

            spec = load_experiment(args.spec)
            run_id = args.run_id or f"run-{uuid.uuid4().hex[:16]}"
            attempt_id = args.attempt_id or f"attempt-{uuid.uuid4().hex[:16]}"
            owner_id = args.owner_id or attempt_id
            output = args.output_root / run_id
            results = ExperimentWorker(
                spec, run_id=run_id, attempt_id=attempt_id, task_uid=owner_id,
                output_root=output,
            ).run()
            emit({"runId": run_id, "attemptId": attempt_id, "ownerId": owner_id,
                  "output": str(output),
                  "succeeded": all(item.status.value == "succeeded" for item in results)})
            return 0 if all(item.status.value == "succeeded" for item in results) else 1
        run_root = args.output_root / args.run_id
        summary = run_root / "summary.json"
        if not summary.exists():
            raise ValueError(f"run summary does not exist: {summary}")
        value = json.loads(summary.read_text(encoding="utf-8"))
        arms = value.get("arms", []) if isinstance(value, dict) else value
        if not isinstance(arms, list):
            raise ValueError(f"run summary has invalid arms: {summary}")
        emit(value if args.command == "collect" else {
            "runId": args.run_id, "output": str(run_root),
            "arms": [{"armId": item.get("arm", {}).get("arm_id"),
                      "status": item.get("status")} for item in arms],
        })
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"clawbox: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
