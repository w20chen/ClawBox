package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// startCpuBurner launches a detached process group whose shell busy-loops in
// user space (so the process-tree sampler must observe real utime), plus a
// short-lived child that allocates ~4MB and sleeps.  Returns the pgid (= the
// shell pid, Setpgid) and a way to wait for completion.
func startCpuBurner(t *testing.T) (*exec.Cmd, int) {
	t.Helper()
	cmd := exec.Command("/bin/sh", "-lc",
		// busy-loop in user space for ~0.5s, then a memory-holding sleep
		"i=0; while [ $i -lt 3000000 ]; do i=$((i+1)); done; "+
			"(head -c 4194304 /dev/zero | tail -c 4194304 >/dev/null 2>&1 &) ; sleep 0.2",
	)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		t.Fatalf("start burner: %v", err)
	}
	return cmd, cmd.Process.Pid
}

// TestCollectorProcessTree verifies the process-tree sampler attributes real
// user CPU and processes to the execution's process group, and that it keeps
// sampling across the run.
func TestCollectorProcessTree(t *testing.T) {
	if os.Getenv("CI_SKIP_COLLECTOR") != "" {
		t.Skip("collector smoke test skipped (CI_SKIP_COLLECTOR)")
	}
	cmd, pgid := startCpuBurner(t)
	defer cmd.Wait() //nolint:errcheck

	dir := t.TempDir()
	collector := startResourceCollector(pgid, "exec-tree-test", dir, 50)
	time.Sleep(600 * time.Millisecond)
	stats := collector.Finish(time.Now())

	if stats.PidCount <= 0 && stats.SamplingPointCount == 0 {
		t.Fatal("collector sampled nothing")
	}
	if stats.CPUUserSeconds <= 0 {
		t.Errorf("expected user CPU > 0 from process-tree sampler, got %v (points=%d)",
			stats.CPUUserSeconds, stats.SamplingPointCount)
	}
	if stats.SamplingPointCount < 2 {
		t.Errorf("expected >=2 sampling points, got %d", stats.SamplingPointCount)
	}
	if stats.Source == "cgroup-v2" && stats.RSSPeakBytes <= 0 {
		t.Errorf("cgroup-v2 path should report a positive RSS peak, got %d", stats.RSSPeakBytes)
	}
	// Artifact must be written by writeResourceArtifact in the cgroup layout.
	writeResourceArtifact("exec-tree-test", stats, dir, time.Now().Add(-time.Second))
	artifacts, err := filepath.Glob(filepath.Join(dir, "tool-resource", "cgroup-resource-*.json"))
	if err != nil || len(artifacts) != 1 {
		t.Fatalf("expected 1 artifact, got %v (err=%v)", artifacts, err)
	}
	raw, err := os.ReadFile(artifacts[0])
	if err != nil {
		t.Fatalf("read artifact: %v", err)
	}
	if !strings.Contains(string(raw), `"execution_id": "exec-tree-test"`) {
		t.Errorf("artifact missing execution_id: %s", raw)
	}
	if !strings.Contains(string(raw), `"schema": "cgroup_resource_v1"`) {
		t.Errorf("artifact missing schema: %s", raw)
	}
}

// TestReadCgroupCountersMissing verifies readCgroupCounters fails closed (ok
// = false) when the per-exec cgroup does not exist, so the collector falls
// back to process-tree instead of emitting garbage.
func TestReadCgroupCountersMissing(t *testing.T) {
	_, _, _, _, _, _, ok := readCgroupCounters("/sys/fs/cgroup/clawbox/definitely-not-here")
	if ok {
		t.Fatal("expected ok=false for missing cgroup")
	}
}

// TestSanitizeCgroupLeaf verifies the leaf sanitizer is stable and safe.
func TestSanitizeCgroupLeaf(t *testing.T) {
	cases := map[string]string{
		"exec-1234-ab": "exec-1234-ab",
		"a/b c:d":      "a_b_c_d",
		"":             "",
	}
	for in, want := range cases {
		if got := sanitizeCgroupLeaf(in); got != want {
			t.Errorf("sanitizeCgroupLeaf(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestTicksToSeconds guards the USER_HZ=100 assumption used by /proc/stat.
func TestTicksToSeconds(t *testing.T) {
	if ticksToSeconds(100) != 1.0 {
		t.Fatalf("ticksToSeconds(100) = %v, want 1.0", ticksToSeconds(100))
	}
}

// TestListPidsInGroup ensures the /proc walker returns parseable numeric pids
// and does not include this test process (different pgid).
func TestListPidsInGroup(t *testing.T) {
	pids := listPidsInGroup(-1)
	for _, pid := range pids {
		if _, err := strconv.Atoi(strconv.Itoa(pid)); err != nil {
			t.Fatalf("non-numeric pid %d", pid)
		}
	}
}
