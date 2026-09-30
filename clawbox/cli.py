"""Standalone CubeSandbox experiment CLI."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from clawbox.experiments import (
    BASELINES, ensure_supported_experiment, expand_matrix, load_experiment,
    spec_digest,
)
from clawbox.experiments.preset_view import DIMENSIONS, dimensions, select_presets
from clawbox.experiments.spec import PolicySpec


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def static_memory_value(value: str) -> int | str:
    if value == "auto":
        return value
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive MiB integer or 'auto'") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive MiB integer or 'auto'")
    return parsed


def positive_float_or_auto(value: str) -> float | str:
    if value == "auto":
        return value
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number or 'auto'") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number or 'auto'")
    return parsed


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
    if value.get("compute_nodes"):
        print(f"Compute placement: {value['placement_policy']} (Runtime + Tool stay together)")
        for node in value["compute_nodes"]:
            print(f"  {node['node_id']}: NUMA {node['numa_node']}, CPUs {node['cpus']}, "
                  f"LOW/HIGH/HARD={node['low_watermark_mib']/1024:g}/"
                  f"{node['high_watermark_mib']/1024:g}/{node['memory_capacity_mib']/1024:g} GiB")
        if value.get("session_compute_nodes"):
            print("  Session node IDs: " + ", ".join(value["session_compute_nodes"]))
        print("  Aggregate capacities below are sums; admission is independent on each compute node.")
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
    admission = value["admission"]
    local_capacity = admission["physical_local_capacity_gib"]
    if admission.get("local_low_watermark_gib") is not None:
        print(
            "Memory scopes: "
            f"LOCAL LOW/HIGH/HARD={admission['local_low_watermark_gib']:g}/"
            f"{admission['local_high_watermark_gib']:g}/{local_capacity:g} GiB, "
            f"shared pool={admission['shared_pool_capacity_gib']:g} GiB, "
            f"live-borrow cap={admission['shared_live_borrow_limit_gib']:g} GiB, "
            f"combined live cgroup limit={admission['combined_live_cgroup_limit_gib']:g} GiB"
        )
    else:
        print(
            "Memory scopes: "
            f"physical LOCAL cgroup={local_capacity if local_capacity is not None else 'unknown'} GiB, "
            f"experiment policy budget={admission['policy_budget_gib']:g} GiB, "
            f"usable after operation headroom={admission['available_after_headroom_gib']:g} GiB"
        )
    static = admission["static"]
    print(
        f"Admission A: fixed={static['per_command_mib']} MiB, "
        f"reservation-only ceiling={static['reservation_only_slots']} concurrent commands"
    )
    p50 = admission.get("p50")
    if p50:
        if p50.get("error"):
            print(f"Admission A+B/C: unavailable ({p50['error']})")
        else:
            print(
                "Admission A+B/C: frozen P50 range="
                f"{p50['min_mib']:g}..{p50['max_mib']:g} MiB "
                f"(median={p50['median_mib']:g}, entries={p50['entry_count']}), "
                "reservation-only ceiling at max="
                f"{p50['reservation_only_slots_at_max']} concurrent commands"
            )
    print("Admission ceilings above exclude measured resident VM memory; run results enforce both.")
    print(f"Policies ({len(value['policies'])}), arms ({value['arm_count']}):")
    for policy in value["policies"]:
        labels = dimensions(PolicySpec.model_validate(policy))
        print(
            f"  {policy['name']}: " + ", ".join(f"{key}={item}" for key, item in labels.items())
        )
    for name, resources in value["effective_policy_resources"].items():
        print(f"  {name}: execution pool={resources['pool_memory_gib']:g} GiB, "
              f"shared pool={resources['shared_pool_gib']:g} GiB, "
              f"snapshot migration={'enabled' if resources['snapshot_enabled'] else 'disabled'}")


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
        if name == "validate":
            command.add_argument("--inputs", action="store_true", help="also parse workload traces and prediction files")
        if name == "run":
            command.add_argument("--run-id")
            command.add_argument("--attempt-id")
            command.add_argument("--owner-id")
            command.add_argument("--qualification", type=Path)
            command.add_argument("--detach", action="store_true")
    resume = sub.add_parser("resume", help="resume non-successful arms from a frozen run")
    resume.add_argument("run_id")
    resume.add_argument("--attempt-id")
    resume.add_argument("--detach", action="store_true")
    host = sub.add_parser("host", help="inspect, configure and check a standalone host before changing it")
    host_actions = host.add_subparsers(dest="host_action", required=True)
    host_actions.add_parser("inspect", help="show NUMA CPUs, RAM and distances without changing the host")
    host_init = host_actions.add_parser("init", help="write an editable host YAML; never overwrite")
    host_init.add_argument("config", type=Path)
    host_init.add_argument("--from-profile", type=Path)
    for action in ("check", "apply"):
        host_command = host_actions.add_parser(action)
        host_command.add_argument("config", type=Path)
        if action == "apply":
            host_command.add_argument("--profile", type=Path,
                                      default=Path.home() / ".config/clawbox/host.json")
    setup = sub.add_parser("setup", help="configure and verify this CubeSandbox host")
    setup.add_argument("--profile", type=Path,
                       default=Path.home() / ".config" / "clawbox" / "host.json")
    setup.add_argument("--runtime-template")
    setup.add_argument("--tool-template")
    setup.add_argument("--node")
    setup.add_argument("--cube-source", help="patched CubeSandbox checkout used to install the matching SDK")
    setup.add_argument("--local-memory-cgroup", default="/sys/fs/cgroup/cube_sandbox/sandbox")
    setup.add_argument("--local-gib", type=int, default=64)
    setup.add_argument("--local-node", type=int, default=0)
    setup.add_argument("--low-gib", type=int)
    setup.add_argument("--high-gib", type=int)
    setup.add_argument("--shared-borrow-percent", type=int, default=50)
    setup.add_argument("--warm", action="store_true")
    setup.add_argument("--warm-root", default="/mnt/clawbox-warm")
    setup.add_argument("--warm-gib", type=int, default=128)
    setup.add_argument("--warm-node", type=int, default=1)
    doctor = sub.add_parser("doctor", help="verify services, templates, memory pool and WARM storage")
    doctor.add_argument("spec", type=Path)
    doctor.add_argument("--probe-vm", action="store_true")
    qualify = sub.add_parser("qualify", help="run a short real-VM gate at target concurrency")
    qualify.add_argument("spec", type=Path)
    qualify.add_argument("--concurrency", type=int)
    qualify.add_argument("--receipt", type=Path)
    launch = sub.add_parser(
        "launch",
        help="validate, refresh host state, qualify when needed, and run or resume",
    )
    launch.add_argument("spec", type=Path)
    launch.add_argument("--run-id")
    launch.add_argument("--qualification", type=Path)
    launch.add_argument("--qualification-concurrency", type=int)
    launch.add_argument("--force-qualify", action="store_true")
    launch.add_argument("--detach", action="store_true")
    importer = sub.add_parser("import-trace", help="convert a supported research trace to replay schema 6")
    importer.add_argument("source", type=Path)
    importer.add_argument("--output", required=True, type=Path)
    importer.add_argument("--python", default="python3")
    trainer = sub.add_parser("train", help="fit frozen P50 LatticeKB/ToolKB predictions from Cube runs")
    trainer.add_argument("runs", nargs="+", type=Path)
    trainer.add_argument("--trace", required=True, type=Path)
    trainer.add_argument("--repository", required=True)
    trainer.add_argument("--output", required=True, type=Path)
    images = sub.add_parser("images", help="rebuild current ClawTune integration images and templates")
    images.add_argument("--profile", type=Path,
                        default=Path.home() / ".config" / "clawbox" / "host.json")
    images.add_argument("--registry")
    images.add_argument("--go")
    images.add_argument("--pip-index", default="https://pypi.org/simple")
    image_network = images.add_mutually_exclusive_group()
    image_network.add_argument("--direct-network", action="store_true", dest="direct_network")
    image_network.add_argument("--proxy-network", action="store_false", dest="direct_network")
    images.set_defaults(direct_network=None)
    images.add_argument("--role", choices=("runtime", "sandbox"), action="append")
    images.add_argument("--kernel-source")
    images.add_argument("--kernel-build")
    for name in ("status", "collect", "report", "abort", "destroy"):
        command = sub.add_parser(name)
        command.add_argument("run_id")
    trace = sub.add_parser("trace", help="inspect a JSONL recording without starting VMs")
    trace.add_argument("path", type=Path)
    describe = sub.add_parser(
        "describe", help="show VM totals, memory overcommit, policies, and arm count",
    )
    describe.add_argument("spec", type=Path)
    describe.add_argument("--json", action="store_true", dest="as_json")
    baselines = sub.add_parser(
        "baselines", help="list complete baseline tuples accepted by configure",
    )
    baselines.add_argument("--all", action="store_true", help="include deprecated policies")
    baselines.add_argument("--json", action="store_true", dest="as_json")
    configure = sub.add_parser(
        "configure", help="create a validated experiment YAML from a checked-in example",
    )
    configure.add_argument("base", type=Path, help="complete schema-v2 YAML to use as a base")
    configure.add_argument("output", type=Path, help="new experiment YAML")
    configure.add_argument(
        "--profile", type=Path,
        default=Path.home() / ".config" / "clawbox" / "host.json",
        help="host profile created by experiment setup; used when present",
    )
    configure.add_argument("--force", action="store_true", help="replace the output file")
    configure.add_argument("--experiment-id")
    for name, choices in DIMENSIONS.items():
        configure.add_argument("--" + name.replace("_", "-"), choices=choices, action="append",
                               help="filter existing presets; repeat for alternatives in this dimension")
    configure.add_argument("--trace")
    configure.add_argument("--case-id")
    configure.add_argument("--prompt")
    configure.add_argument("--repository", help="repository identity stored with the selected case")
    configure.add_argument("--base-commit", help="repository revision expected in the Tool image")
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
    configure.add_argument("--placement-policy", choices=("round_robin", "explicit"))
    configure.add_argument("--session-compute-nodes", help="comma-separated compute node IDs for explicit placement")
    configure.add_argument("--pool-memory-gib", type=float)
    configure.add_argument("--emergency-free-memory-gib", type=float)
    configure.add_argument("--checkpoint-headroom-gib", type=float)
    configure.add_argument(
        "--snapshot-mechanism", choices=("incremental-cow", "full-copy"),
    )
    configure.add_argument(
        "--snapshot-storage", choices=("tiered", "warm-only"),
    )
    configure.add_argument("--warm-root")
    configure.add_argument("--warm-memory-gib", type=float)
    configure.add_argument("--cold-root")
    configure.add_argument(
        "--static-tool-memory-mib", type=static_memory_value,
        help="fixed Tool admission in MiB, or auto for training-artifact P90",
    )
    configure.add_argument("--full-tool-memory-mib", type=int)
    configure.add_argument(
        "--prediction-artifact",
        help="frozen clawbox_p50_v1 file from a separate validated training run",
    )
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
    configure.add_argument(
        "--model-wait-prediction-seconds", type=positive_float_or_auto,
        help="predicted model wait, or auto for the scaled recorded-trace median",
    )
    configure.add_argument("--model-wait-prediction-source")
    configure.add_argument("--fixed-delay-seconds", type=float)
    configure.add_argument("--prefetch-lead-seconds", type=float)
    configure.add_argument("--checkpoint-break-even-seconds", type=float)
    configure.add_argument("--inference-backend", choices=("replay", "api"))
    configure.add_argument("--model")
    configure.add_argument("--base-url")
    configure.add_argument("--api-key-env")
    configure.add_argument(
        "--launch", action="store_true",
        help="validate, qualify, and run the generated spec after writing it",
    )
    configure.add_argument("--run-id", help="run ID used with --launch")
    configure.add_argument(
        "--qualification-concurrency", type=int,
        help="override qualification concurrency used with --launch",
    )
    configure.add_argument(
        "--force-qualify", action="store_true",
        help="rerun qualification even when the existing receipt is valid",
    )
    configure.add_argument(
        "--detach", action="store_true",
        help="start the formal run in the background when used with --launch",
    )
    return root


def launch_experiment(args: argparse.Namespace) -> int:
    """Run the complete safe path while making repeated invocations idempotent."""
    from clawbox.experiments.inputs import validate_inputs
    from clawbox.experiments.qualification import (
        default_receipt_path, qualify, require_host_ready, validate_receipt,
    )
    from clawbox.experiments.reporting import write_run_report_artifacts
    from clawbox.experiments.supervisor import status_for_run

    spec = load_experiment(args.spec)
    ensure_supported_experiment(spec)
    run_id = args.run_id or f"run-{uuid.uuid4().hex[:16]}"
    run_root = args.output_root / run_id
    action = "run"
    if run_root.exists():
        frozen_path = run_root / "experiment.yaml"
        if not frozen_path.is_file():
            raise ValueError(
                f"result directory exists without a frozen experiment: {run_root}"
            )
        frozen = load_experiment(frozen_path)
        if spec_digest(frozen) != spec_digest(spec):
            raise ValueError(
                f"run ID {run_id!r} belongs to a different experiment; "
                "choose another --run-id"
            )
        status = status_for_run(run_root)
        if status.get("supervisor_alive"):
            emit({
                "runId": run_id,
                "state": status.get("state"),
                "output": str(run_root),
                "message": "run is already active",
            })
            return 0
        if status.get("state") == "succeeded":
            report, _memory = write_run_report_artifacts(run_root)
            print(report.read_text(encoding="utf-8"), end="")
            return 0
        action = "resume"

    validate_inputs(spec)
    require_host_ready(spec)

    receipt_path = args.qualification or default_receipt_path(args.spec)
    qualification_reused = False
    if not args.force_qualify:
        try:
            validate_receipt(spec, receipt_path)
            qualification_reused = True
        except (OSError, ValueError):
            pass
    if not qualification_reused:
        qualify(
            spec, output_base=args.output_root,
            receipt_path=receipt_path,
            concurrency=args.qualification_concurrency,
        )

    if action == "run":
        action_args = [
            str(args.spec), "--run-id", run_id,
            "--qualification", str(receipt_path),
        ]
    else:
        action_args = [run_id]
    if args.detach:
        action_args.append("--detach")
    code = main([
        "--output-root", str(args.output_root),
        "experiment", action, *action_args,
    ])
    if code or args.detach:
        return code
    report, _memory = write_run_report_artifacts(run_root)
    print(report.read_text(encoding="utf-8"), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    try:
        if args.command == "trace":
            from clawbox.experiments.inputs import inspect_trace
            emit(inspect_trace(args.path))
            return 0
        if args.command == "import-trace":
            from clawbox.replay.import_trace import import_trace

            emit(import_trace(args.source, args.output, python=args.python))
            return 0
        if args.command == "train":
            from clawbox.experiments.training import train_p50

            report = train_p50(args.runs, args.trace, args.repository, args.output)
            emit(report)
            return 1 if report["unavailable"] else 0
        if args.command == "images":
            repository = Path(__file__).resolve().parents[1]
            script = repository / "scripts" / "refresh-lab-images.py"
            command = [
                sys.executable, str(script), "--profile", str(args.profile),
                "--pip-index", args.pip_index,
                *(["--registry", args.registry] if args.registry else []),
                *(["--go", args.go] if args.go else []),
                *[value for role in args.role or () for value in ("--role", role)],
                *(["--kernel-source", args.kernel_source] if args.kernel_source else []),
                *(["--kernel-build", args.kernel_build] if args.kernel_build else []),
                *(["--direct-network"] if args.direct_network is True else []),
                *(["--proxy-network"] if args.direct_network is False else []),
            ]
            environment = os.environ.copy()
            python_path = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = os.pathsep.join(
                [str(repository), *([python_path] if python_path else [])]
            )
            return subprocess.call(command, env=environment)
        if args.command == "host":
            from clawbox.experiments import host
            if args.host_action == "inspect":
                emit(host.inventory())
            elif args.host_action == "init":
                host.init_config(args.config, args.from_profile)
                emit({"config": args.config, "next": f"Edit {args.config}, then run clawbox experiment host check {args.config}"})
            elif args.host_action == "check":
                report = host.check(host.load_config(args.config))
                emit(report)
                return 0 if report["ready_for_apply"] else 2
            else:
                emit({"ready": True, "profile": host.apply(host.load_config(args.config), args.profile)})
            return 0
        if args.command == "setup":
            from clawbox.lab import setup as setup_host

            profile = setup_host(args)
            emit({"ready": True, "profile": profile})
            return 0
        if args.command == "doctor":
            from clawbox.experiments.qualification import (
                host_profile_for, require_host_ready,
            )
            from clawbox.lab import wait_for_vm_ready

            spec = load_experiment(args.spec)
            ensure_supported_experiment(spec)
            require_host_ready(spec)
            if args.probe_vm:
                wait_for_vm_ready(host_profile_for(spec))
            emit({"ready": True, "specDigest": spec_digest(spec),
                  "vmProbe": bool(args.probe_vm)})
            return 0
        if args.command == "qualify":
            from clawbox.experiments.inputs import validate_inputs
            from clawbox.experiments.qualification import (
                default_receipt_path, qualify,
            )

            spec = load_experiment(args.spec)
            ensure_supported_experiment(spec)
            validate_inputs(spec)
            receipt = qualify(
                spec, output_base=args.output_root,
                receipt_path=args.receipt or default_receipt_path(args.spec),
                concurrency=args.concurrency,
            )
            emit(receipt)
            return 0
        if args.command == "launch":
            return launch_experiment(args)
        if args.command == "baselines":
            admission_required = {
                "tool_full": ["resources.full_tool_memory_mib"],
                "tool_static": ["resources.static_tool_memory_mib"],
                "tool_p50": ["resources.prediction_artifact"],
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
                if value.eviction_policy.value == "time_oracle":
                    required_settings.append("inference.backend=replay")
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
                    labels = dimensions(BASELINES[row['name']].as_policy())
                    requirement = (
                        f", requires {', '.join(row['required_settings'])}"
                        if row["required_settings"] else ""
                    )
                    status = "" if row["status"] == "implemented" else " [DEPRECATED]"
                    print(
                        f"  {row['name']}{status}: "
                        + ", ".join(f"{key}={item}" for key, item in labels.items())
                        + requirement
                    )
            return 0
        if args.command == "configure":
            from clawbox.experiments.configure import (
                configure_experiment, dump_experiment, experiment_overview,
                gib_to_mib,
            )

            if args.output.exists() and not args.force:
                raise ValueError(
                    f"output already exists: {args.output}; pass --force to replace it"
                )
            profile: dict[str, Any] = {}
            if args.profile.is_file():
                try:
                    profile = json.loads(args.profile.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ValueError(f"cannot read host profile {args.profile}: {exc}") from exc
                if not isinstance(profile, dict):
                    raise ValueError(f"host profile must be an object: {args.profile}")
            runtime_profile = profile.get("runtime") or {}
            tool_profile = profile.get("sandbox") or {}
            if not isinstance(runtime_profile, dict) or not isinstance(tool_profile, dict):
                raise ValueError(f"host profile has invalid template records: {args.profile}")
            use_runtime_profile = args.runtime_template_id is None and bool(runtime_profile)
            use_tool_profile = args.tool_template_id is None and bool(tool_profile)
            required_profile_fields = {
                "template_id", "source_image_reference", "image_digest", "vcpu", "memory_mib",
            }
            for label, record, selected in (
                ("runtime", runtime_profile, use_runtime_profile),
                ("sandbox", tool_profile, use_tool_profile),
            ):
                if selected and (missing := sorted(required_profile_fields - set(record))):
                    raise ValueError(
                        f"host profile {label} record is missing: {', '.join(missing)}"
                    )

            runtime_profile_shape_changed = use_runtime_profile and (
                (args.runtime_vcpu is not None
                 and args.runtime_vcpu != runtime_profile.get("vcpu"))
                or (args.runtime_memory_gib is not None
                    and gib_to_mib(args.runtime_memory_gib, name="runtime memory")
                    != runtime_profile.get("memory_mib"))
            )
            tool_profile_shape_changed = use_tool_profile and (
                (args.tool_vcpu is not None
                 and args.tool_vcpu != tool_profile.get("vcpu"))
                or (args.tool_memory_gib is not None
                    and gib_to_mib(args.tool_memory_gib, name="tool memory")
                    != tool_profile.get("memory_mib"))
            )
            if runtime_profile_shape_changed:
                raise ValueError(
                    "changing Runtime CPU or memory requires a new Runtime template"
                )
            if tool_profile_shape_changed:
                raise ValueError(
                    "changing Tool CPU or memory requires a new Tool template"
                )

            spec = configure_experiment(
                args.base,
                compute_nodes=profile.get("compute_nodes"),
                placement_policy=args.placement_policy,
                session_compute_nodes=args.session_compute_nodes.split(",") if args.session_compute_nodes else None,
                experiment_id=args.experiment_id,
                trace=args.trace,
                case_id=args.case_id,
                prompt=args.prompt,
                repository=args.repository,
                base_commit=args.base_commit,
                validation_command=args.validation_command,
                repetitions=args.repetitions,
                session_assignment=args.session_assignment,
                concurrency=args.concurrency,
                baseline_names=select_presets(args.baselines or (), **{
                    name: getattr(args, name) for name in DIMENSIONS}),
                runtime_template_id=(
                    runtime_profile.get("template_id") if use_runtime_profile
                    else args.runtime_template_id
                ),
                tool_template_id=(
                    tool_profile.get("template_id") if use_tool_profile
                    else args.tool_template_id
                ),
                runtime_image_reference=(
                    args.runtime_image_reference
                    if args.runtime_image_reference is not None
                    else runtime_profile.get("source_image_reference")
                    if use_runtime_profile else None
                ),
                tool_image_reference=(
                    args.tool_image_reference
                    if args.tool_image_reference is not None
                    else tool_profile.get("source_image_reference")
                    if use_tool_profile else None
                ),
                runtime_image_digest=(
                    args.runtime_image_digest
                    if args.runtime_image_digest is not None
                    else runtime_profile.get("image_digest")
                    if use_runtime_profile else None
                ),
                tool_image_digest=(
                    args.tool_image_digest
                    if args.tool_image_digest is not None
                    else tool_profile.get("image_digest")
                    if use_tool_profile else None
                ),
                runtime_vcpu=(
                    args.runtime_vcpu
                    if args.runtime_vcpu is not None
                    else runtime_profile.get("vcpu") if use_runtime_profile else None
                ),
                tool_vcpu=(
                    args.tool_vcpu
                    if args.tool_vcpu is not None
                    else tool_profile.get("vcpu") if use_tool_profile else None
                ),
                runtime_memory_gib=(
                    args.runtime_memory_gib
                    if args.runtime_memory_gib is not None
                    else runtime_profile.get("memory_mib", 0) / 1024
                    if use_runtime_profile else None
                ),
                tool_memory_gib=(
                    args.tool_memory_gib
                    if args.tool_memory_gib is not None
                    else tool_profile.get("memory_mib", 0) / 1024
                    if use_tool_profile else None
                ),
                target_node=(
                    args.target_node or profile.get("node") or os.getenv("CUBE_NODE")
                ),
                pool_memory_gib=(
                    args.pool_memory_gib
                    if args.pool_memory_gib is not None else
                    profile.get("local_memory_high_watermark_mib", 0) / 1024
                    if profile.get("local_memory_high_watermark_mib") else None
                ),
                emergency_free_memory_gib=args.emergency_free_memory_gib,
                checkpoint_headroom_gib=args.checkpoint_headroom_gib,
                local_memory_capacity_mib=profile.get("local_memory_capacity_mib"),
                local_memory_low_watermark_mib=profile.get(
                    "local_memory_low_watermark_mib"
                ),
                local_memory_high_watermark_mib=profile.get(
                    "local_memory_high_watermark_mib"
                ),
                shared_memory_borrow_limit_mib=profile.get(
                    "shared_memory_borrow_limit_mib"
                ),
                warm_memory_capacity_mib=(
                    gib_to_mib(args.warm_memory_gib, name="warm memory")
                    if args.warm_memory_gib is not None
                    else profile.get("warm_capacity_mib")
                ),
                local_memory_cgroup=profile.get("local_memory_cgroup"),
                warm_snapshot_root=args.warm_root or profile.get("warm_root"),
                cold_snapshot_root=args.cold_root or profile.get("cold_root"),
                local_numa_node=profile.get("local_numa_node"),
                warm_numa_node=profile.get("warm_numa_node"),
                snapshot_mechanism=args.snapshot_mechanism,
                snapshot_storage=args.snapshot_storage,
                static_tool_memory_mib=args.static_tool_memory_mib,
                full_tool_memory_mib=args.full_tool_memory_mib,
                prediction_artifact=args.prediction_artifact,
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
                checkpoint_break_even_seconds=args.checkpoint_break_even_seconds,
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
            if args.launch:
                launch_args = [
                    "--output-root", str(args.output_root),
                    "experiment", "launch", str(args.output),
                    *(["--run-id", args.run_id] if args.run_id else []),
                    *(
                        ["--qualification-concurrency",
                         str(args.qualification_concurrency)]
                        if args.qualification_concurrency is not None else []
                    ),
                    *(["--force-qualify"] if args.force_qualify else []),
                    *(["--detach"] if args.detach else []),
                ]
                return main(launch_args)
            print(f"Next: clawbox --output-root <results> experiment launch {args.output}")
            return 0
        if args.command == "describe":
            from clawbox.experiments.configure import experiment_overview

            spec = load_experiment(args.spec)
            ensure_supported_experiment(spec)
            value = experiment_overview(spec)
            emit(value) if args.as_json else emit_overview(value)
            return 0
        if args.command in {"validate", "plan"}:
            spec = load_experiment(args.spec)
            ensure_supported_experiment(spec)
            arms = expand_matrix(spec)
            result: dict[str, Any] = {
                "valid": True, "specDigest": spec_digest(spec), "armCount": len(arms),
            }
            if args.command == "validate" and args.inputs:
                from clawbox.experiments.inputs import validate_inputs
                result["inputs"] = validate_inputs(spec)
            if args.command == "plan":
                result["arms"] = [arm.model_dump(mode="json") for arm in arms]
            emit(result)
            return 0
        if args.command in {"run", "resume"}:
            from clawbox.experiments.supervisor import ExperimentSupervisor
            from clawbox.experiments.qualification import (
                default_receipt_path, require_host_ready, validate_receipt,
            )

            run_id = (
                args.run_id if args.command == "resume"
                else args.run_id or f"run-{uuid.uuid4().hex[:16]}"
            )
            output = args.output_root / run_id
            if args.command == "run" and output.exists():
                raise ValueError(
                    f"result directory already exists: {output}; "
                    f"use experiment resume {run_id}"
                )
            spec_path = output / "experiment.yaml" if args.command == "resume" else args.spec
            spec = load_experiment(spec_path)
            ensure_supported_experiment(spec)
            from clawbox.experiments.inputs import validate_inputs
            validate_inputs(spec)
            require_host_ready(spec)
            receipt_path = (
                output / "qualification.json"
                if args.command == "resume"
                else args.qualification or default_receipt_path(args.spec)
            )
            qualification_receipt = validate_receipt(spec, receipt_path)
            attempt_id = args.attempt_id or f"attempt-{uuid.uuid4().hex[:16]}"
            owner_id = (
                str(read_owner_id(output)) if args.command == "resume"
                else args.owner_id or run_id
            )
            if args.detach:
                launch_root = args.output_root / ".launch"
                launch_root.mkdir(parents=True, exist_ok=True)
                log_path = launch_root / f"{run_id}-{attempt_id}.log"
                command = [
                    sys.executable, "-m", "clawbox.cli",
                    "--output-root", str(args.output_root),
                    "experiment", args.command,
                ]
                if args.command == "run":
                    command.extend([
                        str(args.spec), "--run-id", run_id,
                        "--attempt-id", attempt_id, "--owner-id", owner_id,
                        "--qualification", str(receipt_path),
                    ])
                else:
                    command.extend([run_id, "--attempt-id", attempt_id])
                with log_path.open("ab") as log:
                    process = subprocess.Popen(
                        command, stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=(os.name != "nt"),
                        creationflags=(
                            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                        ),
                    )
                emit({
                    "runId": run_id, "attemptId": attempt_id,
                    "ownerId": owner_id, "pid": process.pid,
                    "output": str(output), "launcherLog": str(log_path),
                    "state": "starting",
                })
                return 0
            results = ExperimentSupervisor(
                spec, run_id=run_id, attempt_id=attempt_id,
                owner_id=owner_id, output_root=output,
                resume=args.command == "resume",
                qualification_receipt=qualification_receipt,
            ).run()
            emit({"runId": run_id, "attemptId": attempt_id, "ownerId": owner_id,
                  "output": str(output),
                  "failures": [{"armId": item.arm.arm_id,
                                "reason": item.correctness.get("failure"),
                                "artifacts": item.artifacts}
                               for item in results if item.status.value != "succeeded"],
                  "succeeded": all(item.status.value == "succeeded" for item in results)})
            return 0 if all(item.status.value == "succeeded" for item in results) else 1
        run_root = args.output_root / args.run_id
        if args.command in {"status", "abort", "destroy"}:
            from clawbox.experiments.supervisor import (
                abort_run, cleanup_run, status_for_run,
            )
            if args.command == "status":
                emit(status_for_run(run_root))
                return 0
            if args.command == "abort":
                emit(abort_run(run_root))
                return 0
            state = status_for_run(run_root)
            emit(
                abort_run(run_root) if state["supervisor_alive"]
                else cleanup_run(run_root)
            )
            return 0
        if args.command == "report":
            from clawbox.experiments.reporting import write_run_report_artifacts

            report, _memory = write_run_report_artifacts(run_root)
            print(report.read_text(encoding="utf-8"), end="")
            return 0
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


def read_owner_id(run_root: Path) -> str:
    from clawbox.experiments.supervisor import read_run_state

    value = str(read_run_state(run_root).get("owner_id") or "").strip()
    if not value:
        raise ValueError("run state has no owner_id")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
