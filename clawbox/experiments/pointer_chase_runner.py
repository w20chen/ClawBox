#!/usr/bin/env python3
"""Run four identical native pointer-chasing workers under fixed CPU placements."""

import argparse
import csv
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import time

EVENTS = ("LLC-loads", "LLC-load-misses", "cycles", "instructions")
WORKERS = ("w0", "w1", "w2", "w3")
PLACEMENTS = {
    "same-slice": (8, 10, 12, 14),
    "2-slice": (8, 10, 16, 20),
    "4-slice": (8, 16, 24, 32),
}


def wait_for(path: Path, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {path}")
        time.sleep(0.005)


def parse_perf(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        fields = line.split(",")
        if len(fields) < 3:
            continue
        name = fields[2].strip()
        if name not in EVENTS:
            continue
        raw = fields[0].strip().replace(" ", "")
        try:
            value = float(raw.replace("<notcounted>", "nan").replace("<not supported>", "nan"))
        except ValueError:
            value = float("nan")
        out[name] = value
        if len(fields) >= 5:
            try:
                out[name + "_running_pct"] = float(fields[4].strip())
            except ValueError:
                pass
    return out


def parse_result(path: Path) -> dict:
    result = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        try:
            result[key] = int(value)
        except ValueError:
            result[key] = value
    return result


def start_perf(pid: int, path: Path) -> subprocess.Popen:
    cmd = ["sudo", "-n", "perf", "stat", "-x,", "-o", str(path), "-p", str(pid), "-e", ",".join(EVENTS)]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)


def stop_perf(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def run_trial(binary: str, out_dir: Path, size_mib: int, steps: int, placement: str,
              repeat: int, cpu_offset: int) -> list[dict]:
    trial = out_dir / f"size{size_mib:04d}-{placement}-r{repeat:02d}"
    trial.mkdir(parents=True, exist_ok=True)
    cpus = [x + cpu_offset for x in PLACEMENTS[placement]]
    procs = {}
    ready = {}
    results = {}
    for i, worker in enumerate(WORKERS):
        ready_path = trial / f"{worker}.ready"
        go_path = trial / f"{worker}.go"
        result_path = trial / f"{worker}.result"
        stdout_path = trial / f"{worker}.stdout"
        stderr_path = trial / f"{worker}.stderr"
        cmd = ["taskset", "-c", str(cpus[i]), binary, str(size_mib), str(steps),
               str(0x12340000 + repeat * 97 + i), str(ready_path), str(go_path), str(result_path)]
        stdout = stdout_path.open("w")
        stderr = stderr_path.open("w")
        procs[worker] = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, text=True)
        ready[worker] = (ready_path, go_path, result_path, stdout, stderr)
    try:
        for worker in WORKERS:
            wait_for(ready[worker][0])
        perf = {}
        for worker in WORKERS:
            pid = int(ready[worker][0].read_text().strip())
            perf_path = trial / f"{worker}.perf.csv"
            perf[worker] = (pid, start_perf(pid, perf_path), perf_path)
        # Give perf a scheduling interval to attach before releasing all workers.
        time.sleep(0.25)
        for worker in WORKERS:
            ready[worker][1].touch()
        for worker in WORKERS:
            try:
                procs[worker].wait(timeout=300)
            except subprocess.TimeoutExpired:
                procs[worker].kill()
                procs[worker].wait(timeout=10)
        for worker in WORKERS:
            stop_perf(perf[worker][1])
        rows = []
        for i, worker in enumerate(WORKERS):
            r = parse_result(ready[worker][2])
            p = parse_perf(perf[worker][2])
            row = {"size_mib": size_mib, "steps": steps, "placement": placement,
                   "repeat": repeat, "worker": worker, "cpu": cpus[i], "pid": perf[worker][0]}
            row.update(r)
            row.update(p)
            rows.append(row)
        return rows
    finally:
        for worker in WORKERS:
            for stream in ready.get(worker, (None, None, None, None, None))[3:5]:
                if stream is not None:
                    stream.close()
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def finite(values):
    return [float(v) for v in values if isinstance(v, (int, float)) and v == v]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--sizes-mib", default="1,32")
    ap.add_argument("--steps", type=int, default=80_000_000)
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--cpu-offset", type=int, default=0)
    args = ap.parse_args()
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    sizes = [int(x) for x in args.sizes_mib.split(",") if x]
    all_rows = []
    for size_mib in sizes:
        for placement in PLACEMENTS:
            for repeat in range(args.repeats):
                print(f"size={size_mib}MiB placement={placement} repeat={repeat}", flush=True)
                rows = run_trial(args.binary, out_dir, size_mib, args.steps, placement, repeat, args.cpu_offset)
                all_rows.extend(rows)
    raw_path = out_dir / "workers.jsonl"
    raw_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in all_rows))
    summary = []
    for size_mib in sizes:
        for placement in PLACEMENTS:
            for repeat in range(args.repeats):
                rows = [r for r in all_rows if r["size_mib"] == size_mib and r["placement"] == placement and r["repeat"] == repeat]
                elapsed = finite(r.get("elapsed_ns") for r in rows)
                loads = finite(r.get("LLC-loads") for r in rows)
                misses = finite(r.get("LLC-load-misses") for r in rows)
                cycles = finite(r.get("cycles") for r in rows)
                instr = finite(r.get("instructions") for r in rows)
                summary.append({
                    "size_mib": size_mib, "steps": args.steps, "placement": placement, "repeat": repeat,
                    "workers": len(rows), "makespan_ns": max(elapsed) if elapsed else None,
                    "median_worker_ns": statistics.median(elapsed) if elapsed else None,
                    "llc_loads": sum(loads) if loads else None, "llc_misses": sum(misses) if misses else None,
                    "llc_miss_ratio": (sum(misses) / sum(loads)) if loads and sum(loads) else None,
                    "cycles": sum(cycles) if cycles else None, "instructions": sum(instr) if instr else None,
                    "min_running_pct": min((v for e in EVENTS for v in finite(r.get(e + "_running_pct") for r in rows)), default=None),
                })
    with (out_dir / "summary.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary[0]))
        writer.writeheader(); writer.writerows(summary)
    (out_dir / "metadata.json").write_text(json.dumps({"binary": args.binary, "sizes_mib": sizes,
        "steps": args.steps, "repeats": args.repeats, "cpu_offset": args.cpu_offset,
        "placements": PLACEMENTS, "events": EVENTS}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
