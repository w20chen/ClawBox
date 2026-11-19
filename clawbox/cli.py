"""Standalone CubeSandbox experiment CLI."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from clawbox.experiments import (
    ensure_supported_experiment, expand_matrix, load_experiment,
    spec_digest,
)
from clawbox.experiments.spec import ExperimentSpec, PolicySpec


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
    if 'dimensions' in value:
        print(f"Experiment: {value['experiment_id']} | baseline: {value['arms'][0]['name']} | trials: {value['arm_count']}")
        dependency_limit = value["execution"].get(
            "dependency_install_concurrency"
        )
        print(
            "Dependency installs: unlimited"
            if dependency_limit is None
            else f"Dependency installs: max {dependency_limit} active (explicit gate)"
        )
        print('Arm | Admission | Runtime | Placement | Idle reclaim | Shared memory | Checkpoint | Deployment | Coordination')
        print('Fixed conditions: ' + json.dumps(value['conditions'], sort_keys=True))
        for arm in value['arms']:
            m = arm['mechanisms']
            print(' | '.join((arm['name'],
                m['admission']['tool'],
                f"memory={m['runtime']['memory_protection']} cpu={m['runtime']['cpu_release_boost']} reclaim={m['runtime']['proactive_memory_reclaim']}",
                m['placement']['policy'] + f" affinity={m['placement']['template_affinity']}",
                m['idle_reclaim']['policy'] + '/' + m['idle_reclaim']['scope'],
                m['shared_memory']['borrow'],
                '/'.join(m['checkpoint'][k] for k in ('mechanism', 'storage', 'residency')),
                m['deployment']['mode'], m['coordination']['action_selection'])))
            for key, item in arm['differences'].items():
                print(f'  {key} = {item}')
            for warning in arm['warnings']:
                print(f'  Warning: {warning}')
        print('Use describe --json for full effective resources and evidence; launch verifies prerequisites.')
        return



def configuration_arguments(command):
    command.add_argument('spec', type=Path, nargs='?', help='internal resolved run snapshot')
    command.add_argument('--baseline', type=Path, help='baseline YAML with all system and policy settings')
    command.add_argument('--workload', type=Path, help='workload YAML with tasks and submission settings')


def configured_spec(args):
    from clawbox.experiments.configuration import load_configuration
    baseline, workload = getattr(args, 'baseline', None), getattr(args, 'workload', None)
    if baseline is not None or workload is not None:
        if baseline is None or workload is None or args.spec is not None:
            raise ValueError('supply both --baseline and --workload, without a positional snapshot')
        return load_configuration(baseline, workload)
    if args.spec is None:
        raise ValueError('supply --baseline BASELINE.yaml --workload WORKLOAD.yaml')
    return load_experiment(args.spec)


def materialize_configuration(args, spec):
    from clawbox.experiments.configuration import dump_experiment
    if getattr(args, 'baseline', None) is None:
        return args.spec
    directory = args.output_root / '.configurations'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (spec_digest(spec) + '.yaml')
    if not path.exists():
        # An immutable, content-addressed snapshot feeds the existing launcher.
        try:
            temporary = directory / (path.name + '.' + uuid.uuid4().hex + '.tmp')
            temporary.write_text(dump_experiment(spec), encoding='utf-8')
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
    if spec_digest(load_experiment(path)) != spec_digest(spec):
        raise ValueError(f'configuration snapshot mismatch: {path}')
    return path


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--output-root", type=Path,
                      default=Path(os.getenv("CLAWBOX_OUTPUT_ROOT", "results")))
    commands = root.add_subparsers(dest="group", required=True)
    experiment = commands.add_parser("experiment")
    sub = experiment.add_subparsers(dest="command", required=True)
    schema = sub.add_parser('schema', help='print baseline or workload JSON Schema')
    schema.add_argument('kind', choices=('baseline', 'workload'))
    resolve = sub.add_parser('resolve', help='freeze a baseline and workload without launching VMs')
    configuration_arguments(resolve)
    resolve.add_argument('--output', type=Path, required=True)
    cache = sub.add_parser('base-cache', help='prepare fixed shared-DRAM template RAM caches before VM arrivals')
    cache_actions = cache.add_subparsers(dest='cache_action', required=True)
    cache_init = cache_actions.add_parser('init', help='resolve selected template RAM files from native catalogs into a reviewable YAML')
    cache_init.add_argument('config', type=Path)
    cache_init.add_argument('--baseline', type=Path, required=True)
    cache_init.add_argument('--workload', type=Path, required=True)
    cache_init.set_defaults(spec=None)
    cache_init.add_argument('--template', action='append', required=True)
    cache_init.add_argument('--capacity-mib', type=int, required=True)
    cache_init.add_argument('--vmm-binary', help='explicit installed Shim file for nonstandard component roots; default: template catalog component version')
    cache_prepare = cache_actions.add_parser('prepare', help='preview a cache YAML; --apply prewarms and activates it on an idle host')
    cache_prepare.add_argument('config', type=Path)
    cache_prepare.add_argument('--apply', action='store_true')
    cache_status = cache_actions.add_parser('status', help='verify active cache identities and report actual allocation')
    cache_status.add_argument('--verify-bytes', action='store_true', help='also hash source and cache files; preparation cost, not a per-VM check')
    cache_cleanup = cache_actions.add_parser('cleanup', help='preview owned cache cleanup; original SSD templates are preserved')
    cache_cleanup.add_argument('--apply', action='store_true')
    cache_cleanup.add_argument('--config', type=Path, help='also recover interrupted preparations owned by this exact plan')
    for name in ("validate", "plan", "run"):
        command = sub.add_parser(name)
        configuration_arguments(command)
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
    host_init.add_argument("--detect", action="store_true", help="propose topology and machine settings from this Linux host; review before apply")
    clean = host_actions.add_parser("clean", help="preview old kernel/systemd crash dumps; never prune VM or experiment data")
    clean.add_argument("--apply", action="store_true", help="delete the listed matching dumps")
    clean.add_argument("--root", type=Path, action="append", help="dedicated crash directory; default: /var/crash and /var/lib/systemd/coredump")
    clean.add_argument("--older-than-hours", type=int, default=24)
    clean.add_argument("--min-size-mib", type=int, default=100)
    for action in ("check", "apply"):
        host_command = host_actions.add_parser(action)
        host_command.add_argument("config", type=Path)
        if action == "apply":
            host_command.add_argument("--profile", type=Path,
                                      default=Path.home() / ".config/clawbox/host.json")
    snapshot_storage = sub.add_parser(
        "snapshot-storage",
        help="prepare local SSD domains and a private peer network",
    )
    snapshot_storage_actions = snapshot_storage.add_subparsers(
        dest="snapshot_storage_action", required=True,
    )
    snapshot_storage_init = snapshot_storage_actions.add_parser(
        "init", help="write an editable storage plan; never overwrite",
    )
    snapshot_storage_init.add_argument("config", type=Path)
    for action, help_text in (
        ("check", "verify prerequisites and show the proposed plan"),
        ("apply", "apply or recover an exactly matching reviewed plan"),
    ):
        command = snapshot_storage_actions.add_parser(action, help=help_text)
        command.add_argument("config", type=Path)
    snapshot_storage_cleanup = snapshot_storage_actions.add_parser(
        "cleanup", help="preview cleanup; preserve loop image files",
    )
    snapshot_storage_cleanup.add_argument("config", type=Path)
    snapshot_storage_cleanup.add_argument(
        "--apply", action="store_true",
        help="unmount owned storage and detach loops; image files remain",
    )
    snapshot_storage_prepare = snapshot_storage_actions.add_parser(
        "prepare", help="import complete immutable templates into applied local SSD domains",
    )
    snapshot_storage_prepare.add_argument("config", type=Path)
    snapshot_storage_prepare.add_argument("--template", action="append", required=True,
                                          help="verified template ID; repeat for Runtime and every Tool template")
    snapshot_storage_prepare.add_argument("--control-socket", required=True,
                                          help="absolute path to the installed native peer control socket")
    snapshot_storage_prepare.add_argument("--receipt", type=Path, required=True,
                                          help="new preparation receipt path; never overwrite")
    snapshot_storage_prepare.add_argument("--workers", type=int, default=1,
                                          help="positive number of concurrent template preparations (default: 1)")
    snapshot_network = snapshot_storage_actions.add_parser(
        "qualify-network", help="measure two overlapping TCP transfers and verify an aggregate rate cap",
    )
    snapshot_network.add_argument("config", type=Path)
    snapshot_network.add_argument("--output", type=Path, required=True,
                                  help="new immutable transport qualification JSON")
    snapshot_network.add_argument("--size-mib", type=int, default=128,
                                  help="size of each of two temporary objects in MiB (default: 128)")
    snapshot_replicas = snapshot_storage_actions.add_parser(
        "cleanup-replicas", help="preview run-owned replica cleanup after all VMs stop",
    )
    snapshot_replicas.add_argument("config", type=Path)
    snapshot_replicas.add_argument("--run-id", required=True)
    snapshot_replicas.add_argument("--control-socket", required=True)
    snapshot_replicas.add_argument("--apply", action="store_true")
    snapshot_reserve = snapshot_storage_actions.add_parser(
        "reserve-image", help="reserve a new image in a reviewed XFS AG and verify its physical SSD extents",
    )
    snapshot_reserve.add_argument("--image", type=Path, required=True)
    snapshot_reserve.add_argument("--capacity-mib", type=int, required=True)
    snapshot_reserve.add_argument("--backing-device", required=True)
    snapshot_reserve.add_argument("--physical-device", required=True)
    snapshot_reserve.add_argument("--allocation-group", type=int, required=True)
    snapshot_reserve.add_argument("--allocation-group-end", type=int)
    snapshot_reserve.add_argument("--chunk-mib", type=int, default=16384)
    snapshot_reserve.add_argument("--resume-receipt", type=Path)
    snapshot_inspect = snapshot_storage_actions.add_parser(
        "inspect-backing", help="read XFS allocation-group free space and physical SSD mapping")
    snapshot_inspect.add_argument("--backing-device", required=True)
    snapshot_reset = snapshot_storage_actions.add_parser(
        'reset-cache', help='preview owned SSD page-cache residency; --apply requests narrow advisory eviction between arms')
    snapshot_reset.add_argument('config', type=Path)
    snapshot_reset.add_argument('--apply', action='store_true')
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
    configuration_arguments(doctor)
    doctor.add_argument("--probe-vm", action="store_true")
    qualify = sub.add_parser("qualify", help="run a bounded real-VM capability smoke")
    configuration_arguments(qualify)
    qualify.add_argument("--concurrency", type=int)
    qualify.add_argument("--receipt", type=Path)
    sharing = sub.add_parser(
        "prove-template-sharing",
        help="verify every Tool template once and freeze reusable physical-sharing evidence",
    )
    configuration_arguments(sharing)
    sharing.add_argument("--output", required=True, type=Path)
    sharing.add_argument("--artifact-root", type=Path)
    sharing.add_argument(
        "--resume-result", action="append", default=[], type=Path,
        help="reuse verified per-template mappings from a prior raw result.json; repeat for multiple attempts",
    )
    launch = sub.add_parser(
        "launch",
        help="validate, refresh host state, qualify when needed, and run or resume",
    )
    configuration_arguments(launch)
    launch.add_argument("--apply-host", type=Path, metavar="HOST.yaml",
                        help="apply a reviewed host YAML before a new/resumed run; save the verified profile beside it as .json; requires an idle VM host")
    launch.add_argument("--run-id")
    launch.add_argument("--qualification", type=Path)
    launch.add_argument("--qualification-concurrency", type=int,
                        help="bound simultaneous smoke sessions (default: minimum of target concurrency, sandbox-create concurrency, and sampled case count); samples up to qualification_case_limit cases, does not prove target density")
    launch.add_argument("--force-qualify", action="store_true")
    launch.add_argument("--detach", action="store_true")
    importer = sub.add_parser("import-trace", help="convert a supported research trace to replay schema 6")
    importer.add_argument("source", type=Path)
    importer.add_argument("--output", required=True, type=Path)
    importer.add_argument("--python", default="python3")
    sim_inputs = sub.add_parser("prepare-serving-sim", help="prepare GPU replay inputs and a trace-free CPU workload/manifest")
    configuration_arguments(sim_inputs)
    sim_inputs.add_argument("--output", required=True, type=Path)
    sim_inputs.add_argument("--source-map", type=Path, help="JSON object mapping case IDs to original format-5 files; relative paths use this file's directory")
    importer.add_argument(
        "--source-workspace", default="/testbed",
        help="absolute workspace path recorded by the source trace (default: /testbed)",
    )
    importer.add_argument(
        "--target-workspace", default="/workspace",
        help="OpenClaw workspace path exposed to replayed tools (default: /workspace)",
    )
    corpus = sub.add_parser(
        "import-corpus",
        help="import unique SWE-rebench traces and write a prioritized workload manifest",
    )
    corpus.add_argument("source", type=Path)
    corpus.add_argument("--output", required=True, type=Path)
    corpus.add_argument("--count", type=int)
    corpus.add_argument(
        "--prefer", action="append", default=None,
        help="preferred project substring; repeat to set priority order (default: pennylane, sqlglot)",
    )
    corpus.add_argument("--python", default="python3")
    corpus.add_argument("--source-workspace", default="/testbed")
    corpus.add_argument("--target-workspace", default="/workspace")
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
    images.add_argument("--tool-prewarm", choices=("enabled", "disabled"),
                        help="compile Tool BPF before template readiness (default: enabled)")
    images.add_argument("--tool-prewarm-timeout-seconds", type=int,
                        help="Tool template initialization deadline (default: 120 seconds)")
    for name in ("status", "collect", "report", "abort", "destroy"):
        command = sub.add_parser(name)
        command.add_argument("run_id")
        if name in {"abort", "destroy"}:
            command.add_argument(
                "--cleanup-workers", type=int, default=8,
                help="maximum concurrent Cube destroy requests (default: 8)",
            )
            command.add_argument(
                "--cleanup-timeout-seconds", type=float, default=180.0,
                help="deadline for owned-VM cleanup in seconds (default: 180)",
            )
    trace = sub.add_parser("trace", help="inspect a JSONL recording without starting VMs")
    trace.add_argument("path", type=Path)
    describe = sub.add_parser(
        "describe", help="show VM totals, memory overcommit, policies, and arm count",
    )
    configuration_arguments(describe)
    describe.add_argument("--json", action="store_true", dest="as_json")

    return root


def launch_experiment(args: argparse.Namespace) -> int:
    """Run the complete safe path while making repeated invocations idempotent."""
    from clawbox.experiments.qualification import (
        default_receipt_path, qualify, require_host_ready, validate_receipt,
    )
    from clawbox.experiments.reporting import write_run_report_artifacts
    from clawbox.experiments.supervisor import status_for_run

    spec = configured_spec(args)
    args.spec = materialize_configuration(args, spec)
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

    receipt_path = getattr(args, "qualification", None) or default_receipt_path(materialize_configuration(args, spec))
    qualification_reused = False
    if not getattr(args, "force_qualify", False):
        try:
            validate_receipt(spec, receipt_path)
            qualification_reused = True
        except (OSError, ValueError) as exc:
            if getattr(args, "qualification", None) is not None:
                raise ValueError(
                    f"explicit qualification receipt cannot be reused: {receipt_path}: {exc}. "
                    "Pass a valid receipt, omit --qualification to qualify automatically, "
                    "or use --force-qualify to replace it."
                ) from exc
    validated_inputs = None
    if not qualification_reused or getattr(args, 'apply_host', None) is not None:
        from clawbox.experiments.inputs import validate_inputs, validate_launch_inputs
        validated_inputs = (
            validate_inputs(spec)
            if getattr(args, 'apply_host', None) is not None
            else validate_launch_inputs(spec)
        )
    if getattr(args, 'apply_host', None) is not None:
        # Host mutation remains behind a complete input check. Normal receipt
        # reuse takes the byte-hash fast path above.
        from clawbox.experiments.host import apply, load_config
        host_path = args.apply_host
        if host_path.suffix.lower() not in {'.yaml', '.yml'}:
            raise ValueError('--apply-host requires a .yaml or .yml host configuration')
        apply(load_config(host_path), host_path.with_suffix('.json'))
    require_host_ready(spec)

    if not qualification_reused:
        qualify(
            spec, output_base=args.output_root,
            receipt_path=receipt_path,
            concurrency=args.qualification_concurrency,
            validated_inputs=validated_inputs,
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

            emit(import_trace(
                args.source, args.output, python=args.python,
                source_workspace=args.source_workspace,
                target_workspace=args.target_workspace,
            ))
            return 0
        if args.command == "prepare-serving-sim":
            from clawbox.experiments.serving_sim_inputs import prepare_serving_sim

            sources = None
            if args.source_map:
                sources = json.loads(args.source_map.read_text(encoding="utf-8"))
                if not isinstance(sources, dict) or not all(isinstance(v, str) for v in sources.values()):
                    raise ValueError("--source-map requires a JSON object of case ID to source file path")
                sources = {key: str((args.source_map.resolve().parent / value).resolve()) for key, value in sources.items()}
            emit(prepare_serving_sim(configured_spec(args), args.output, source_map=sources))
            return 0
        if args.command == "import-corpus":
            from clawbox.replay.import_corpus import prepare_corpus

            emit(prepare_corpus(
                args.source, args.output, count=args.count,
                preferred_projects=args.prefer or ("pennylane", "sqlglot"),
                python=args.python, source_workspace=args.source_workspace,
                target_workspace=args.target_workspace,
            ))
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
                *(["--tool-prewarm", args.tool_prewarm] if args.tool_prewarm else []),
                *(["--tool-prewarm-timeout-seconds", str(args.tool_prewarm_timeout_seconds)]
                  if args.tool_prewarm_timeout_seconds is not None else []),
                *(["--direct-network"] if args.direct_network is True else []),
                *(["--proxy-network"] if args.direct_network is False else []),
            ]
            environment = os.environ.copy()
            python_path = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = os.pathsep.join(
                [str(repository), *([python_path] if python_path else [])]
            )
            return subprocess.call(command, env=environment)
        if args.command == "base-cache":
            from clawbox.experiments import base_cache
            if args.cache_action == "init":
                spec = configured_spec(args)
                emit(base_cache.init_plan(args.config, spec_path=materialize_configuration(args, spec), templates=args.template,
                                         capacity_mib=args.capacity_mib, vmm_binary=args.vmm_binary))
            elif args.cache_action == "prepare":
                emit(base_cache.prepare(base_cache.load_plan(args.config), apply=args.apply))
            elif args.cache_action == "status":
                emit(base_cache.status(full=args.verify_bytes))
            else:
                emit(base_cache.cleanup(apply=args.apply, plan=base_cache.load_plan(args.config) if args.config else None))
            return 0
        if args.command == "host":
            from clawbox.experiments import host
            if args.host_action == "inspect":
                emit(host.inventory())
            elif args.host_action == "init":
                host.init_config(args.config, args.from_profile, detect=args.detect)
                emit({"config": args.config, "next": f"Edit {args.config}, then run clawbox experiment host check {args.config}"})
            elif args.host_action == "clean":
                from clawbox.experiments.disk_cleanup import DEFAULT_ROOTS, clean_dumps
                report = clean_dumps(args.root if args.root is not None else DEFAULT_ROOTS,
                                    apply=args.apply, older_than_hours=args.older_than_hours,
                                    min_size_mib=args.min_size_mib)
                emit(report)
                return 1 if report['errors'] else 0
            elif args.host_action == "check":
                report = host.check(host.load_config(args.config))
                emit(report)
                return 0 if report["ready_for_apply"] else 2
            else:
                emit({"ready": True, "profile": host.apply(host.load_config(args.config), args.profile)})
            return 0
        if args.command == "snapshot-storage":
            from clawbox.experiments import snapshot_host

            if args.snapshot_storage_action == "inspect-backing":
                from clawbox.experiments.physical_storage import inspect_backing
                emit(inspect_backing(args.backing_device))
                return 0
            if args.snapshot_storage_action == "reserve-image":
                from clawbox.experiments.physical_storage import allocate_image, allocate_chunked_image
                if args.capacity_mib < 512:
                    raise ValueError("reserved image capacity must be at least 512 MiB")
                if args.resume_receipt and args.allocation_group_end is None:
                    raise ValueError("--resume-receipt requires --allocation-group-end")
                allocator = allocate_image if args.allocation_group_end is None else allocate_chunked_image
                extra = {} if args.allocation_group_end is None else {
                    "allocation_group_end": args.allocation_group_end, "chunk_bytes": args.chunk_mib * 1024**2,
                    "resume_receipt": args.resume_receipt}
                emit(allocator(args.image, args.capacity_mib * 1024**2,
                    args.backing_device, args.physical_device, args.allocation_group, **extra))
                return 0
            if args.snapshot_storage_action == "init":
                snapshot_host.init(args.config)
                emit({
                    "config": str(args.config),
                    "next": (
                        "Review the plan, then run: scripts/clawbox --sudo "
                        f"experiment snapshot-storage check {args.config}"
                    ),
                })
                return 0
            storage_spec = snapshot_host.load(args.config)
            if args.snapshot_storage_action == "check":
                report = snapshot_host.check(storage_spec)
                emit(report)
                return 0 if report["ready_for_apply"] else 2
            if args.snapshot_storage_action == "apply":
                emit(snapshot_host.apply(storage_spec))
                return 0
            if args.snapshot_storage_action == "qualify-network":
                emit(snapshot_host.qualify_network(storage_spec, args.output, size_mib=args.size_mib))
                return 0
            if args.snapshot_storage_action == "cleanup-replicas":
                emit(snapshot_host.cleanup_replicas(storage_spec, args.run_id,
                    args.control_socket, execute=args.apply))
                return 0
            if args.snapshot_storage_action == 'reset-cache':
                from clawbox.experiments.peer_cache import reset_cache
                emit(reset_cache(storage_spec, execute=args.apply))
                return 0
            if args.snapshot_storage_action == "prepare":
                from clawbox.cube.peer_replica import prepare_templates
                from clawbox.experiments.spec import SnapshotSchedulingStorageSpec
                if args.workers < 1:
                    raise ValueError("snapshot-storage prepare --workers must be a positive integer")
                report = snapshot_host.check(storage_spec)
                if not report["ready_for_apply"]:
                    emit(report)
                    return 2
                snapshot_host.validate_peer_storage(SnapshotSchedulingStorageSpec(
                    **snapshot_host.storage_configuration(storage_spec),
                    host_receipt=storage_spec.receipt,
                    native_control_socket=args.control_socket,
                ))
                # Reserve the evidence filename before doing any imports.
                args.receipt.parent.mkdir(parents=True, exist_ok=True)
                with args.receipt.open("x", encoding="utf-8") as receipt:
                    try:
                        result = prepare_templates(
                            list(dict.fromkeys(args.template)),
                            {node: item.local_mount for node, item in storage_spec.nodes.items()},
                            args.control_socket, storage_spec.shared_dram.root, workers=args.workers,
                        )
                    except Exception as exc:
                        json.dump({"status": "failed", "error": str(exc)}, receipt, indent=2)
                        raise
                    json.dump(result, receipt, indent=2)
                emit(result)
                return 0
            emit(snapshot_host.cleanup(storage_spec, execute=args.apply))
            return 0
        if args.command == "setup":
            from clawbox.experiments.host_runtime import setup as setup_host

            profile = setup_host(args)
            emit({"ready": True, "profile": profile})
            return 0
        if args.command == "doctor":
            from clawbox.experiments.qualification import (
                host_profile_for, require_host_ready,
            )
            from clawbox.experiments.host_runtime import wait_for_vm_ready

            spec = configured_spec(args)
            ensure_supported_experiment(spec)
            require_host_ready(spec)
            if args.probe_vm:
                wait_for_vm_ready(host_profile_for(spec))
            emit({"ready": True, "specDigest": spec_digest(spec),
                  "vmProbe": bool(args.probe_vm)})
            return 0
        if args.command == "qualify":
            from clawbox.experiments.qualification import (
                default_receipt_path, qualify,
            )

            spec = configured_spec(args)
            ensure_supported_experiment(spec)
            receipt = qualify(
                spec, output_base=args.output_root,
                receipt_path=args.receipt or default_receipt_path(materialize_configuration(args, spec)),
                concurrency=args.concurrency,
            )
            emit(receipt)
            return 0
        if args.command == "prove-template-sharing":
            from clawbox.experiments.qualification import (
                require_host_ready, template_base_sharing_test,
                write_template_base_sharing_artifact,
            )

            spec = configured_spec(args)
            ensure_supported_experiment(spec)
            if spec.schema_version == 4:
                design = deepcopy(spec.design)
                design["evidence"]["template_base_sharing_artifact"] = None
                spec = ExperimentSpec.model_validate(design)
            else:
                spec = spec.model_copy(update={
                    "resources": spec.resources.model_copy(update={
                        "template_base_sharing_artifact": None,
                    }),
                })
            # A proof-producing spec normally points at the artifact that this
            # command is about to create.  Host validation must not try to
            # consume that future path before proof generation starts.
            require_host_ready(spec)
            artifact_root = args.artifact_root or args.output.with_name(
                args.output.stem + "-raw"
            )
            if artifact_root.exists():
                raise FileExistsError(
                    f"refusing to replace template-sharing proof root {artifact_root}"
                )
            result = template_base_sharing_test(
                spec, artifact_root=artifact_root,
                prior_results=tuple(args.resume_result),
            )
            payload = write_template_base_sharing_artifact(
                spec, result, artifact_root=artifact_root, output=args.output,
            )
            emit({
                "output": str(args.output),
                "artifact_root": str(artifact_root),
                "template_count": payload["template_count"],
                "backend_identity": payload["backend_identity"],
            })
            return 0
        if args.command == "launch":
            return launch_experiment(args)
        if args.command == 'schema':
            from clawbox.experiments.configuration import BaselineConfig, WorkloadConfig
            emit((BaselineConfig if args.kind == 'baseline' else WorkloadConfig).model_json_schema())
            return 0
        if args.command == 'resolve':
            from clawbox.experiments.configuration import dump_experiment
            if args.output.exists():
                raise ValueError(f'refusing to overwrite {args.output}; choose a new output path')
            spec = configured_spec(args)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open('x', encoding='utf-8') as stream:
                stream.write(dump_experiment(spec))
            emit({'output': str(args.output), 'specDigest': spec_digest(spec)})
            return 0
        if args.command == "describe":
            from clawbox.experiments.configuration import experiment_overview

            spec = configured_spec(args)
            ensure_supported_experiment(spec)
            value = experiment_overview(spec)
            emit(value) if args.as_json else emit_overview(value)
            return 0
        if args.command in {"validate", "plan"}:
            spec = configured_spec(args)
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
            if args.command == 'resume':
                spec = load_experiment(output / 'experiment.yaml')
            else:
                spec = configured_spec(args)
                args.spec = materialize_configuration(args, spec)
            ensure_supported_experiment(spec)
            if args.command == "run":
                # Keep direct lower-level use actionable without repeating the
                # expensive full corpus audit performed only by validate --inputs.
                from clawbox.experiments.inputs import validate_launch_inputs
                validate_launch_inputs(spec)
            require_host_ready(spec)
            receipt_path = (
                output / "qualification.json"
                if args.command == "resume"
                else args.qualification or default_receipt_path(materialize_configuration(args, spec))
            )
            qualification_receipt = validate_receipt(spec, receipt_path)
            if args.command == 'resume':
                from clawbox.experiments.supervisor import read_run_state
                attempt_id = args.attempt_id or read_run_state(output)['attempt_id']
            else:
                attempt_id = args.attempt_id or f"attempt-{uuid.uuid4().hex[:16]}"
            owner_id = (
                str(read_owner_id(output)) if args.command == "resume"
                else args.owner_id or run_id
            )
            if args.detach:
                launch_root = args.output_root / ".launch"
                launch_root.mkdir(parents=True, exist_ok=True)
                log_path = launch_root / f"{run_id}-{attempt_id}-{uuid.uuid4().hex[:8]}.log"
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
                emit(abort_run(
                    run_root, cleanup_workers=args.cleanup_workers,
                    cleanup_timeout_seconds=args.cleanup_timeout_seconds,
                ))
                return 0
            state = status_for_run(run_root)
            emit(
                abort_run(
                    run_root, cleanup_workers=args.cleanup_workers,
                    cleanup_timeout_seconds=args.cleanup_timeout_seconds,
                ) if state["supervisor_alive"] else cleanup_run(
                    run_root, cleanup_workers=args.cleanup_workers,
                    cleanup_timeout_seconds=args.cleanup_timeout_seconds,
                )
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
        if args.command == "collect":
            from clawbox.experiments.results import load_summary_arm_result

            expanded = [load_summary_arm_result(run_root, item) for item in arms]
            emit({**value, "arms": expanded} if isinstance(value, dict) else expanded)
            return 0
        emit({
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
