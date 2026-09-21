"""Run the frozen same-checkpoint replica placement experiment on kunpeng."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(os.environ.get("PLACEMENT_REPLICA_ROOT", "/tmp/placement-replica-formal-20260914"))
PLAN_PATH = ROOT / "plan.json"
PLAN = json.loads(PLAN_PATH.read_text())
PLAN_HASH = hashlib.sha256(PLAN_PATH.read_bytes()).hexdigest()
CPU_OFFSET = int(os.environ.get("PLACEMENT_REPLICA_CPU_OFFSET", "0"))
MEM_NODE = int(os.environ.get("PLACEMENT_REPLICA_MEM_NODE", "0"))
RUN_LABEL = os.environ.get("PLACEMENT_REPLICA_RUN_LABEL", f"node{MEM_NODE}")
POOL = [int(x) + CPU_OFFSET for x in PLAN["topology"]["linux_pool"]]
PLACEMENTS = PLAN["placements"]
PLACEMENT_VARIANTS = PLAN.get("placement_variants", {})
WORKERS = ("w0", "w1", "w2", "w3")
MEMORY_BYTES = 6 * 1024**3
EVENTS = "LLC-loads,LLC-load-misses,cycles,instructions"
TARGET_TIMEOUT = 600
PIDS_LIMIT = os.environ.get("PLACEMENT_REPLICA_PIDS_LIMIT", "512")
ACTIVE_PLACEMENTS = tuple(
    x for x in os.environ.get("PLACEMENT_REPLICA_PLACEMENTS", "same-slice,2-slice,4-slice").split(",") if x
)
REPEATS = int(os.environ.get("PLACEMENT_REPLICA_REPEATS", str(PLAN["repeats"])))


def run(*args, timeout=120, check=True):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=check)


def expand(spec):
    out = []
    for part in spec.split(","):
        if not part:
            continue
        edge = part.split("-")
        out.extend(range(int(edge[0]), int(edge[-1]) + 1))
    return sorted(out)


def cgroup_state(name, intended):
    pid = int(run("docker", "inspect", "-f", "{{.State.Pid}}", name).stdout)
    status = Path(f"/proc/{pid}/status").read_text()
    match = re.search(r"^Cpus_allowed_list:\s*(\S+)", status, re.M)
    if not match:
        raise RuntimeError(f"missing Cpus_allowed_list for {name}")
    rel = Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[1]
    cgroup = Path("/sys/fs/cgroup" + rel)
    allowed = expand(match.group(1))
    effective = expand((cgroup / "cpuset.cpus.effective").read_text().strip())
    mems = expand((cgroup / "cpuset.mems.effective").read_text().strip())
    maxmem = (cgroup / "memory.max").read_text().strip()
    expected = sorted(int(x) for x in intended)
    if allowed != expected or effective != expected or mems != [MEM_NODE] or maxmem != str(MEMORY_BYTES):
        raise RuntimeError(
            f"cgroup mismatch {name}: allowed={allowed} intended={expected} "
            f"effective={effective} mems={mems} maxmem={maxmem}"
        )
    return {
        "host_init_pid": pid,
        "cgroup_path": str(cgroup),
        "allowed_host_cpus": allowed,
        "effective_cpus": effective,
        "effective_mems": mems,
        "expected_mem_node": MEM_NODE,
        "memory_max_bytes": int(maxmem),
    }


def host_wrapper_pid(name):
    rows = run("docker", "top", name, "-eo", "pid,args").stdout.splitlines()[1:]
    found = [int(row.split()[0]) for row in rows if "/input/wrapper.sh" in row]
    if not found:
        raise RuntimeError(f"wrapper PID ambiguous in {name}: {rows}")
    # qemu-user can expose both the exec shim and the actual wrapper with the
    # same command line.  The newest PID is the live wrapper process; either
    # process belongs to the same container cgroup.
    return max(found)


def mapping(placement, round_id):
    if placement == "linux":
        return {worker: list(POOL) for worker in WORKERS}
    if placement in PLACEMENT_VARIANTS:
        variants = PLACEMENT_VARIANTS[placement]
        variant = variants[(round_id - 1) % len(variants)]
        workers = variant["workers"] if isinstance(variant, dict) else variant
        return {
            worker: [int(cpu) + CPU_OFFSET for cpu in workers[idx]]
            for idx, worker in enumerate(WORKERS)
        }
    groups = PLACEMENTS[placement]["workers"]
    offset = (round_id + list(PLACEMENTS).index(placement)) % 4
    return {
        worker: [int(groups[idx][(offset + idx) % len(groups[idx])]) + CPU_OFFSET]
        for idx, worker in enumerate(WORKERS)
    }


def parse_perf(text):
    wanted = {"LLC-loads", "LLC-load-misses", "cycles", "instructions"}
    result = {}
    for line in text.splitlines():
        fields = line.split(",")
        if len(fields) < 5:
            continue
        value, event = fields[0].strip(), fields[2].strip()
        if event not in wanted:
            continue
        # perf's CSV has one extra cgroup column when -G is used.  In both
        # layouts the final percentage column is time_running/time_enabled;
        # accepting only 100% prevents multiplexed or partial samples.
        running_pct = fields[5].strip() if len(fields) > 5 else fields[4].strip()
        if value.startswith("<") or running_pct != "100.00":
            raise RuntimeError(f"PMU event not fully counted: {line!r}")
        result[event] = int(value.replace(" ", ""))
    missing = wanted - set(result)
    if missing:
        raise RuntimeError(f"PMU events missing: {sorted(missing)}; raw={text!r}")
    return result


def parse_running_ratios(text):
    wanted = {"LLC-loads", "LLC-load-misses", "cycles", "instructions"}
    result = {}
    for line in text.splitlines():
        fields = line.split(",")
        if len(fields) < 6:
            continue
        event = fields[2].strip()
        if event in wanted:
            result[event] = float(fields[5].strip()) / 100.0
    if set(result) != wanted:
        raise RuntimeError(f"PMU running ratios missing: {sorted(wanted - set(result))}; raw={text!r}")
    return result


def read_cgroup_memory(cgroup):
    """Capture cgroup v2 memory counters without treating them as working set."""
    def read_int(name):
        value = (cgroup / name).read_text().strip()
        return None if value == "max" else int(value)

    stat = {}
    for line in (cgroup / "memory.stat").read_text().splitlines():
        fields = line.split()
        if len(fields) == 2:
            try:
                stat[fields[0]] = int(fields[1])
            except ValueError:
                stat[fields[0]] = fields[1]
    return {
        "memory_current_bytes": read_int("memory.current"),
        "memory_peak_bytes": read_int("memory.peak"),
        "memory_stat": stat,
    }


def target_env(target):
    args = []
    for key, value in target.get("environment", {}).items():
        args.extend(["-e", f"{key}={value}"])
    args.extend(["-w", target.get("workdir", "/workspace")])
    return args


def output_contract_ok(target, stdout):
    """Check a stable status line while allowing pytest/pip timing noise."""
    text = stdout.decode("utf-8", errors="replace")
    lines = [line.strip() for line in target.get("expected_stdout_tail", "").splitlines() if line.strip()]
    if not lines or not text.strip():
        return bool(text.strip())
    expected = lines[-1]
    normalized_expected = re.sub(r"\d+(?:\.\d+)?s\b", "<duration>", expected)
    normalized_expected = re.sub(r"\([0-9a-f]{7,64}\)", "(<id>)", normalized_expected)
    normalized_text = re.sub(r"\d+(?:\.\d+)?s\b", "<duration>", text)
    normalized_text = re.sub(r"\([0-9a-f]{7,64}\)", "(<id>)", normalized_text)
    return normalized_expected in normalized_text


def output_digest_ok(target, stdout):
    expected = target.get("expected_stdout_sha256")
    if not expected:
        return True
    observed = hashlib.sha256(stdout).hexdigest()
    if observed == expected:
        return True
    # Trace exports and Docker-mounted result files disagree on whether the
    # final stdout line includes its conventional newline. Treat only this
    # exact representation difference as equivalent.
    return hashlib.sha256(stdout.rstrip(b"\r\n")).hexdigest() == expected


def run_trial(candidate, round_id, placement, attempt=1, warmup=False):
    target = PLAN["targets"][candidate]
    name_tag = "warm" if warmup else f"r{round_id}"
    trial_name = f"{candidate}-{name_tag}-{placement}-{attempt}"
    trial_dir = ROOT / trial_name
    trial_dir.mkdir(exist_ok=False)
    barrier = trial_dir / "barrier"
    barrier.mkdir()
    mapped = mapping(placement, round_id)
    variant = None
    if placement in PLACEMENT_VARIANTS:
        variant = PLACEMENT_VARIANTS[placement][(round_id - 1) % len(PLACEMENT_VARIANTS[placement])]
    containers, runners, perf, states = {}, {}, {}, {}
    try:
        image_id = run("docker", "image", "inspect", "-f", "{{.Id}}", target["checkpoint_image"]).stdout.strip()
        if image_id != target["checkpoint_image_id"]:
            raise RuntimeError(f"checkpoint image id changed: {image_id}")
        for worker in WORKERS:
            leaf = trial_dir / worker
            input_dir, result_dir = leaf / "input", leaf / "result"
            input_dir.mkdir(parents=True)
            result_dir.mkdir()
            (input_dir / "target.sh").write_text(target["command"], encoding="utf-8")
            wrapper = (
                "set +e\n"
                "touch /result/ready\n"
                "while [ ! -e /barrier/go ]; do sleep 0.02; done\n"
                "start=$(date +%s%N)\n"
                "bash /input/target.sh > /result/stdout 2> /result/stderr\n"
                "code=$?\n"
                "end=$(date +%s%N)\n"
                "printf '%s %s %s\\n' \"$start\" \"$end\" \"$code\" > /result/timing\n"
                "exit \"$code\"\n"
            )
            (input_dir / "wrapper.sh").write_text(wrapper, encoding="utf-8")
            container = f"placement-replica-{candidate}-{trial_name}-{worker}"
            cpus = ",".join(str(x) for x in mapped[worker])
            args = [
                "docker", "run", "--platform", "linux/amd64", "-d", "--name", container,
                "--cpuset-cpus", cpus, "--cpuset-mems", str(MEM_NODE), "--memory", "6g",
                "--memory-swap", "6g", "--network", "none",
                "-v", f"{input_dir}:/input:ro", "-v", f"{result_dir}:/result",
                "-v", f"{barrier}:/barrier:ro", "--pids-limit", PIDS_LIMIT,
            ] + target_env(target) + [target["checkpoint_image"], "sleep", "infinity"]
            run(*args, timeout=90)
            containers[worker] = container
            states[worker] = cgroup_state(container, mapped[worker])
            live_id = run("docker", "inspect", "-f", "{{.Image}}", container).stdout.strip()
            if live_id != image_id:
                raise RuntimeError(f"live image mismatch for {candidate}/{worker}")
            states[worker]["checkpoint_image_id"] = live_id
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                worker: pool.submit(
                    subprocess.Popen, ["docker", "exec", containers[worker], "bash", "/input/wrapper.sh"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                for worker in WORKERS
            }
            runners = {worker: future.result() for worker, future in futures.items()}
        deadline = time.monotonic() + 60
        while not all((trial_dir / worker / "result" / "ready").exists() for worker in WORKERS):
            if time.monotonic() > deadline:
                raise TimeoutError("barrier readiness timeout")
            time.sleep(0.02)
        for worker in WORKERS:
            states[worker]["host_wrapper_pid"] = host_wrapper_pid(containers[worker])
            cgroup = Path(states[worker]["cgroup_path"])
            states[worker]["cpu_stat_before"] = (cgroup / "cpu.stat").read_text()
            if not warmup:
                perf_file = trial_dir / worker / "perf.csv"
                perf[worker] = subprocess.Popen(
                    [
                        "sudo", "-n", "perf", "stat", "-x,", "-o", str(perf_file), "-a",
                        "-C", ",".join(str(x) for x in mapped[worker]), "-e", EVENTS,
                        "-G", str(cgroup.relative_to("/sys/fs/cgroup")),
                    ],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        time.sleep(0.20)
        (barrier / "go").touch()
        for proc in runners.values():
            proc.wait(timeout=TARGET_TIMEOUT)
        for proc in perf.values():
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
            proc.wait(timeout=20)

        replicas = {}
        for worker in WORKERS:
            leaf = trial_dir / worker
            start_ns, end_ns, code = map(int, (leaf / "result" / "timing").read_text().split())
            stdout = (leaf / "result" / "stdout").read_bytes()
            stderr = (leaf / "result" / "stderr").read_text(errors="replace")
            stdout_sha = hashlib.sha256(stdout).hexdigest()
            if (
                code != target["expected_exit_code"]
                or not output_contract_ok(target, stdout)
                or not output_digest_ok(target, stdout)
                or "Segmentation fault" in stderr
            ):
                raise RuntimeError(
                    f"target mismatch {candidate}/{worker}: code={code} sha={stdout_sha} "
                    f"expected_status={target.get('expected_stdout_tail', '')!r} stderr={stderr[-500:]!r}"
                )
            cgroup = Path(states[worker]["cgroup_path"])
            states[worker]["cpu_stat_after"] = (cgroup / "cpu.stat").read_text()
            states[worker]["memory_numa_stat"] = (cgroup / "memory.numa_stat").read_text()
            states[worker]["memory"] = read_cgroup_memory(cgroup)
            perf_text = (leaf / "perf.csv").read_text() if not warmup else ""
            pmu = parse_perf(perf_text) if not warmup else None
            pmu_running_ratio = parse_running_ratios(perf_text) if not warmup else None
            replicas[worker] = {
                "started_s": start_ns / 1e9, "ended_s": end_ns / 1e9,
                "duration_s": (end_ns - start_ns) / 1e9, "exit_code": code,
                "stdout_sha256": stdout_sha,
                "output_digest_match": output_digest_ok(target, stdout),
                "checkpoint_verified": True,
                "mapping_verified": True, "intended_cpus": mapped[worker],
                "allowed_host_cpus": states[worker]["allowed_host_cpus"],
                "effective_cpus": states[worker]["effective_cpus"],
                "effective_mems": states[worker]["effective_mems"],
                "host_wrapper_pid": states[worker]["host_wrapper_pid"],
                "checkpoint_image_id": states[worker]["checkpoint_image_id"],
                "cpu_stat_before": states[worker]["cpu_stat_before"],
                "cpu_stat_after": states[worker]["cpu_stat_after"],
                "memory_numa_stat": states[worker]["memory_numa_stat"],
                "memory_current_bytes": states[worker]["memory"]["memory_current_bytes"],
                "memory_peak_bytes": states[worker]["memory"]["memory_peak_bytes"],
                "memory_stat": states[worker]["memory"]["memory_stat"],
                "pmu": pmu, "pmu_running_ratio": pmu_running_ratio,
                "perf_stat": perf_text or None,
            }
        starts = [x["started_s"] for x in replicas.values()]
        ends = [x["ended_s"] for x in replicas.values()]
        row = {
            "schema": "placement_replica_trial_v2", "plan_sha256": PLAN_HASH,
            "candidate": candidate, "R_i": target["predicted_rate"], "R_band": target["band"],
            "run_label": RUN_LABEL, "numa_node": MEM_NODE, "cpu_offset": CPU_OFFSET,
            "round": round_id, "placement": placement, "attempt": attempt,
            "placement_variant": variant.get("id") if isinstance(variant, dict) else None,
            "placement_workers": variant.get("workers") if isinstance(variant, dict) else mapped,
            "status": "warmup" if warmup else "verified", "replicas": replicas,
            "makespan_s": max(ends) - min(starts), "overlap_s": min(ends) - max(starts),
        }
        (trial_dir / "trial.json").write_text(json.dumps(row, indent=2) + "\n")
        return row
    finally:
        for proc in perf.values():
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for proc in runners.values():
            if proc.poll() is None:
                proc.terminate()
        for container in containers.values():
            run("docker", "rm", "-f", container, timeout=30, check=False)


def run_one(candidate, round_id, placement, warmup=False):
    for attempt in range(1, 4):
        trial_dir = ROOT / f"{candidate}-{'warm' if warmup else 'r' + str(round_id)}-{placement}-{attempt}"
        trial_file = trial_dir / "trial.json"
        if trial_file.exists():
            try:
                previous = json.loads(trial_file.read_text())
                if previous.get("status") in {"verified", "warmup"}:
                    print(json.dumps({"candidate": candidate, "round": round_id,
                                      "placement": placement, "attempt": attempt,
                                      "status": "resumed", "makespan_s": previous.get("makespan_s")},
                                     sort_keys=True), flush=True)
                    return previous
            except (OSError, json.JSONDecodeError):
                pass
        if trial_dir.exists():
            continue
        try:
            row = run_trial(candidate, round_id, placement, attempt, warmup)
            print(json.dumps({
                "candidate": candidate, "round": round_id, "placement": placement,
                "attempt": attempt, "status": row["status"], "makespan_s": row["makespan_s"],
                "replica_s": {k: v["duration_s"] for k, v in row["replicas"].items()},
            }, sort_keys=True), flush=True)
            return row
        except Exception as error:
            print(f"failed {candidate} r{round_id} {placement} attempt {attempt}: {error}", file=sys.stderr, flush=True)
            if attempt == 3:
                raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--candidate", choices=sorted(PLAN["targets"]))
    parser.add_argument("--round", type=int)
    parser.add_argument("--placement", choices=sorted(PLACEMENTS))
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.warmup:
        for candidate in sorted(PLAN["targets"]):
            for attempt in (1, 2):
                row = run_trial(candidate, 0, "linux", attempt=attempt, warmup=True)
                print(json.dumps({"candidate": candidate, "round": 0, "placement": "linux",
                                  "attempt": attempt, "status": row["status"],
                                  "makespan_s": row["makespan_s"]}, sort_keys=True), flush=True)
        return
    if args.all:
        out = ROOT / "rounds.jsonl"
        requested = os.environ.get("PLACEMENT_REPLICA_CANDIDATES")
        candidates = [x for x in (requested.split(",") if requested else sorted(PLAN["targets"])) if x]
        with out.open("a", encoding="utf-8") as stream:
            for round_id in range(1, REPEATS + 1):
                order = list(ACTIVE_PLACEMENTS)
                shift = (PLAN["random_seed"] + round_id) % len(order)
                order = order[shift:] + order[:shift]
                for candidate in candidates:
                    for placement in order:
                        row = run_one(candidate, round_id, placement)
                        stream.write(json.dumps(row) + "\n")
                        stream.flush()
        return
    if args.candidate and args.round and args.placement:
        run_one(args.candidate, args.round, args.placement)
        return
    parser.error("specify --warmup, --all, or --candidate --round --placement")


if __name__ == "__main__":
    main()
