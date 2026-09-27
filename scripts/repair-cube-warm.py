#!/usr/bin/env python3
"""Apply and validate the required EINTR repair in the installed incremental Cubelet."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import time

from cubesandbox import Sandbox

HELPER = '''package cubebox

import "golang.org/x/sys/unix"

var warmFallocateCall = unix.Fallocate

// Repeating the same allocation is idempotent, including after partial progress.
// A Go runtime signal may interrupt fallocate on a large tmpfs range.
func warmFallocate(fd int, mode uint32, offset, length int64) error {
    for {
        err := warmFallocateCall(fd, mode, offset, length)
        if err != unix.EINTR { return err }
    }
}
'''

TEST = '''package cubebox

import (
    "testing"
    "golang.org/x/sys/unix"
)

func TestWarmFallocateRetriesOnlyEINTR(t *testing.T) {
    original := warmFallocateCall
    defer func() { warmFallocateCall = original }()
    calls := 0
    warmFallocateCall = func(fd int, mode uint32, offset, length int64) error {
        calls++
        if fd != 7 || mode != 0 || offset != 4096 || length != 8192 { t.Fatal("range changed") }
        if calls < 3 { return unix.EINTR }
        return nil
    }
    if err := warmFallocate(7, 0, 4096, 8192); err != nil || calls != 3 { t.Fatalf("%v %d", err, calls) }
    warmFallocateCall = func(int, uint32, int64, int64) error { return unix.ENOSPC }
    if err := warmFallocate(7, 0, 4096, 8192); err != unix.ENOSPC { t.Fatal(err) }
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=os.getenv("CUBE_SOURCE_DIR"))
    parser.add_argument("--go", default=os.getenv("CLAWBOX_GO", "go"))
    parser.add_argument("--cow-sdk", default=os.getenv("CLAWBOX_COW_SDK"))
    args = parser.parse_args()
    if not args.source:
        parser.error("Set CUBE_SOURCE_DIR or --source to the installed incremental CubeSandbox source")
    if any(row.get("state") == "running" for row in Sandbox.list_v2()):
        raise RuntimeError("Repair requires an idle VM host; existing VMs will not be stopped")
    root = Path(args.source).resolve()/"Cubelet"
    if args.cow_sdk:
        cow = Path(args.cow_sdk).resolve()
        if not (cow/"include/cubecow.h").is_file() or not (cow/"lib/libcubecow.a").is_file():
            parser.error("--cow-sdk must contain the matching include/cubecow.h and lib/libcubecow.a")
        for part in ("include", "lib"):
            target = root/"third_party/cubecow"/part
            if not target.exists():
                subprocess.run(["sudo", "-n", "ln", "-s", str(cow/part), str(target)], check=True)
    package = root/"services/cubebox"
    updates = {}
    for name in ("incremental_memory.go", "pause_cow.go"):
        path = package/name
        text = path.read_text()
        if "unix.Fallocate(" not in text and "warmFallocate(" not in text:
            raise RuntimeError(f"Unsupported source layout: {path}")
        text = text.replace("unix.Fallocate(", "warmFallocate(")
        if "unix." not in text:
            text = text.replace('\t"golang.org/x/sys/unix"\n', '')
        updates[path] = text
    updates[package/"warm_fallocate.go"] = HELPER
    updates[package/"warm_fallocate_test.go"] = TEST
    for path, text in updates.items():
        path.write_text(text)
    subprocess.run([str(Path(args.go).with_name("gofmt")), "-w", *map(str, updates)], check=True)
    env = dict(os.environ, GOCACHE=str(Path.home()/".cache/clawbox/go-build"))
    subprocess.run([args.go, "test", "-mod=readonly", "./services/cubebox", "-run", "TestWarmFallocate|TestServeWarmAllocation", "-count=1"], cwd=root, env=env, check=True)
    with tempfile.TemporaryDirectory(prefix="clawbox-cubelet-") as temp:
        binary = Path(temp)/"cubelet"
        subprocess.run([args.go, "build", "-mod=readonly", "-o", str(binary), "./cmd/cubelet"], cwd=root, env=env, check=True)
        if any(row.get("state") == "running" for row in Sandbox.list_v2()):
            raise RuntimeError("VMs started during build; refusing to replace Cubelet")
        installed = Path("/usr/local/services/cubetoolbox/Cubelet/bin/cubelet")
        subprocess.run(["sudo", "-n", "cp", "-p", str(installed), str(installed)+".pre-lab-"+str(time.time_ns())], check=True)
        subprocess.run(["sudo", "-n", "systemctl", "stop", "cube-sandbox-cubelet"], check=True)
        try:
            subprocess.run(["sudo", "-n", "install", "-m", "755", str(binary), str(installed)], check=True)
        finally:
            subprocess.run(["sudo", "-n", "systemctl", "start", "cube-sandbox-cubelet"], check=True)
    print("Cubelet EINTR repair installed. Run scripts/lab setup --warm to wait for node readiness.")


if __name__ == "__main__":
    main()
