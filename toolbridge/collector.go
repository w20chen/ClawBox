package main

// Per-execution resource collector for the Tool VM.
//
// Two independent measurement paths, both emitting the ClawTune-compatible
// ``cgroup_resource_v1`` artifact (see clawbox/tuning/schema.py and ClawTune
// services/sidecar/src/clawtune_sidecar/telemetry/cgroup_resource.py):
//
//   * ``cgroup-v2``    — when the guest /sys/fs/cgroup is writable: create a
//                        per-execution cgroup, move the execution process
//                        group into it, then read cpu.stat / memory.peak /
//                        io.stat / pids.current.  Exact counters, immune to
//                        process lifetime.
//   * ``process-tree`` — always-available fallback: periodic /proc sampling of
//                        the execution's process group (utime/stime, VmRSS,
//                        io bytes).  Requires no privileges and no kernel
//                        features; a small amount of CPU/mem of processes that
//                        exit between samples can be missed.
//
// The bridge runs the tool command as ``/bin/sh -lc`` with Setpgid, so the
// whole tool process tree shares one process-group id (the shell pid).  The
// sampler walks /proc for every process in that group, which is exactly the
// set of processes that belong to this execution — and only this execution.
//
// Rationale for guest-side collection (see docs/GAPS.md G0): the host OS only
// sees the whole Firecracker microVM as one process, so per-command attribution
// is impossible from the host.  The ClawTune sidecar in the Runtime VM only
// sees the SSH client, not the tool command.  The Tool VM is the only place
// that observes the real tool processes.

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// resourceStats is the merged per-execution measurement.  It mirrors the
// ClawTune cgroup_resource_v1 fields (CgroupResourceResult) so the existing
// ClawBox pipeline (schema.py / dataset.py / join.py) parses it verbatim.
type resourceStats struct {
	Source            string  `json:"source"`             // cgroup-v2 | process-tree
	MonitorSource     string  `json:"monitor_source"`     // cgroup-v2 | psutil-process-tree
	AttributionSource string  `json:"attribution_source"` // tool-bridge-pgid
	TsStart           float64 `json:"ts_start"`
	TsEnd             float64 `json:"ts_end"`
	DurationMS        int64   `json:"duration_ms"`
	CPUUserSeconds    float64 `json:"cpu_user_seconds"`
	CPUSystemSeconds  float64 `json:"cpu_system_seconds"`
	CPUTimeSeconds    float64 `json:"cpu_time_seconds"`
	RSSBeforeBytes    int64   `json:"rss_before_bytes"`
	RSSAfterBytes     int64   `json:"rss_after_bytes"`
	RSSPeakBytes      int64   `json:"rss_peak_bytes"`
	ReadBytesDelta    int64   `json:"read_bytes_delta"`
	WriteBytesDelta   int64   `json:"write_bytes_delta"`
	PidCount          int     `json:"pid_count"`
	SamplingIntervalMS int64  `json:"sampling_interval_ms"`
	SamplingPointCount int   `json:"sampling_point_count"`
	SamplingQuality   string  `json:"sampling_quality"`
}

type resourceCollector struct {
	mu         sync.Mutex
	execID     string
	traceDir   string
	pgid       int
	intervalMS int
	done       chan struct{}

	// process-tree accumulators
	cpuUserSeconds float64
	cpuSysSeconds  float64
	rssPeakKiB     int64
	rssBeforeKiB   int64
	rssAfterKiB    int64
	readBytes      int64
	writeBytes     int64
	pointCount     int
	lastUser       map[int]int64 // pid -> last utime ticks
	lastSys        map[int]int64 // pid -> last stime ticks
	lastRead       map[int]int64 // pid -> last read_bytes
	lastWrite      map[int]int64 // pid -> last write_bytes

	// cgroup v2 path ("" when unavailable)
	cgroupPath string
	cgroupOK   bool
}

func resourceTraceDir() string {
	if path := os.Getenv("TOOL_BRIDGE_LOG_PATH"); path != "" {
		return filepath.Dir(path)
	}
	return "/testbed/.clawbox"
}

// readProcStat parses /proc/<pid>/stat, returning (state, pgrp, utimeTicks,
// stimeTicks).  comm may contain spaces/parens, so we split after the last ')'.
func readProcStat(pid int) (state string, pgrp int, utime, stime int64, ok bool) {
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return "", 0, 0, 0, false
	}
	raw := string(data)
	close := strings.LastIndexByte(raw, ')')
	if close < 0 || close+2 >= len(raw) {
		return "", 0, 0, 0, false
	}
	fields := strings.Fields(raw[close+2:])
	// fields[0]=state(f3) fields[1]=ppid(f4) fields[2]=pgrp(f5)
	// fields[11]=utime(f14) fields[12]=stime(f15)
	if len(fields) < 13 {
		return "", 0, 0, 0, false
	}
	pgrp, _ = strconv.Atoi(fields[2])
	utime, _ = strconv.ParseInt(fields[11], 10, 64)
	stime, _ = strconv.ParseInt(fields[12], 10, 64)
	return fields[0], pgrp, utime, stime, true
}

// readProcStatusVMRSS reads the current VmRSS in KiB from /proc/<pid>/status.
func readProcStatusVMRSS(pid int) int64 {
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/status", pid))
	if err != nil {
		return 0
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(line, "VmRSS:") {
			fields := strings.Fields(line)
			if len(fields) >= 2 {
				kb, _ := strconv.ParseInt(fields[1], 10, 64)
				return kb
			}
		}
	}
	return 0
}

// readProcIO reads read_bytes / write_bytes from /proc/<pid>/io.
func readProcIO(pid int) (readBytes, writeBytes int64) {
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/io", pid))
	if err != nil {
		return 0, 0
	}
	for _, line := range strings.Split(string(data), "\n") {
		switch {
		case strings.HasPrefix(line, "read_bytes:"):
			readBytes, _ = strconv.ParseInt(strings.TrimSpace(strings.TrimPrefix(line, "read_bytes:")), 10, 64)
		case strings.HasPrefix(line, "write_bytes:"):
			writeBytes, _ = strconv.ParseInt(strings.TrimSpace(strings.TrimPrefix(line, "write_bytes:")), 10, 64)
		}
	}
	return readBytes, writeBytes
}

func ticksToSeconds(ticks int64) float64 { return float64(ticks) / 100.0 } // USER_HZ=100

// scanProcessTree samples every process currently in the execution's process
// group and updates the accumulator with per-pid deltas (so CPU/IO is not
// double counted across samples).
func (c *resourceCollector) scanProcessTree() {
	var rssSum int64
	pids := descendantPids(c.pgid) // c.pgid is the execution's shell pid (root)
	first := c.pointCount == 0
	for _, pid := range pids {
		state, _, utime, stime, ok := readProcStat(pid)
		if !ok {
			continue
		}
		if state == "Z" {
			continue // zombies have no live resource attribution
		}
		rssSum += readProcStatusVMRSS(pid)
		readBytes, writeBytes := readProcIO(pid)

		if first {
			c.lastUser[pid] = utime
			c.lastSys[pid] = stime
			c.lastRead[pid] = readBytes
			c.lastWrite[pid] = writeBytes
			continue
		}
		if prev, seen := c.lastUser[pid]; seen && utime >= prev {
			c.cpuUserSeconds += ticksToSeconds(utime - prev)
		}
		if prev, seen := c.lastSys[pid]; seen && stime >= prev {
			c.cpuSysSeconds += ticksToSeconds(stime - prev)
		}
		if prev, seen := c.lastRead[pid]; seen && readBytes >= prev {
			c.readBytes += readBytes - prev
		}
		if prev, seen := c.lastWrite[pid]; seen && writeBytes >= prev {
			c.writeBytes += writeBytes - prev
		}
		c.lastUser[pid] = utime
		c.lastSys[pid] = stime
		c.lastRead[pid] = readBytes
		c.lastWrite[pid] = writeBytes
	}
	c.pointCount++
	if c.pointCount == 1 {
		c.rssBeforeKiB = rssSum
	}
	c.rssAfterKiB = rssSum // last sample of the run
	if rssSum > c.rssPeakKiB {
		c.rssPeakKiB = rssSum
	}
}

// descendantPids returns root and all its descendants by walking
// /proc/<pid>/task/<pid>/children.  Robust against guest process-group quirks
// (the Kata guest keeps container processes in pgrp 0, so Setpgid and
// pgrp-based filtering are unreliable there).
func descendantPids(root int) []int {
	seen := make(map[int]bool)
	var out []int
	var walk func(int)
	walk = func(pid int) {
		if pid <= 0 || seen[pid] {
			return
		}
		seen[pid] = true
		out = append(out, pid)
		data, err := os.ReadFile(fmt.Sprintf("/proc/%d/task/%d/children", pid, pid))
		if err != nil {
			return
		}
		for _, field := range strings.Fields(string(data)) {
			child, _ := strconv.Atoi(field)
			walk(child)
		}
	}
	walk(root)
	return out
}

// startResourceCollector begins periodic process-tree sampling of the
// execution's process group.  pgid is the process-group id of the execution
// (the /bin/sh pid, since the bridge spawns it with Setpgid).  Call Finish()
// after cmd.Wait() returns.
func startResourceCollector(pgid int, execID, traceDir string, intervalMS int) *resourceCollector {
	if intervalMS <= 0 {
		intervalMS = 100
	}
	c := &resourceCollector{
		execID:     execID,
		traceDir:   traceDir,
		pgid:       pgid,
		intervalMS: intervalMS,
		done:       make(chan struct{}),
		lastUser:   map[int]int64{},
		lastSys:    map[int]int64{},
		lastRead:   map[int]int64{},
		lastWrite:  map[int]int64{},
	}
	// Attempt per-execution cgroup v2 (best effort; process-tree always runs).
	if path, ok := tryPerExecCgroup(execID, pgid); ok {
		c.cgroupPath = path
		c.cgroupOK = true
	}
	go func() {
		ticker := time.NewTicker(time.Duration(c.intervalMS) * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				c.mu.Lock()
				c.scanProcessTree()
				c.mu.Unlock()
			case <-c.done:
				return
			}
		}
	}()
	return c
}

// tryPerExecCgroup creates /sys/fs/cgroup/clawbox/<sanitized-exec-id>, moves
// the execution's process group into it, and returns the cgroup path on
// success.  Any failure (read-only mount, missing perms, ...) returns ok=false
// so the caller falls back to process-tree.  NOTE: the pids written into
// cgroup.procs are exactly the pids in the group at this moment; children
// forked afterwards inherit the cgroup automatically.
//
// The Kata guest mounts cgroup2 read-only (probe-verified: `ro` in
// /proc/mounts), so the bridge first remounts it rw and enables the memory
// controller for the subtree (both best-effort; they need CAP_SYS_ADMIN,
// which the tool container is granted).  Without the memory controller only
// cpu/io are exact and RSS falls back to the process-tree sampler.
func tryPerExecCgroup(execID string, shellPid int) (string, bool) {
	remountCgroupRW()
	base := "/sys/fs/cgroup/clawbox"
	leaf := sanitizeCgroupLeaf(execID)
	if leaf == "" {
		return "", false
	}
	if err := os.MkdirAll(base, 0755); err != nil {
		return "", false
	}
	// Enable cpu/memory/pids/io for the clawbox subtree so per-exec cgroups
	// expose cpu.stat / memory.peak / pids.current.  One write per controller
	// (best-effort; already-enabled ones fail harmlessly and are ignored).
	for _, ctl := range []string{"+cpu", "+memory", "+pids", "+io"} {
		_ = os.WriteFile("/sys/fs/cgroup/cgroup.subtree_control", []byte(ctl), 0644)
		_ = os.WriteFile(filepath.Join(base, "cgroup.subtree_control"), []byte(ctl), 0644)
	}
	path := filepath.Join(base, leaf)
	if err := os.MkdirAll(path, 0755); err != nil {
		return "", false
	}
	// Move the execution's shell into the per-exec cgroup; descendants inherit
	// the cgroup at fork, so one write suffices.  Writes the shell pid
	// directly and does NOT rely on the guest honoring process groups (the
	// Kata guest keeps container processes in pgrp 0).
	if err := os.WriteFile(filepath.Join(path, "cgroup.procs"), []byte(strconv.Itoa(shellPid)+"\n"), 0644); err != nil {
		_ = os.RemoveAll(path)
		return "", false
	}
	return path, true
}

// remountCgroupRW best-effort remounts the guest cgroup2 tree read-write.
func remountCgroupRW() {
	_ = exec.Command("mount", "-o", "remount,rw", "/sys/fs/cgroup").Run()
}

func sanitizeCgroupLeaf(value string) string {
	var b strings.Builder
	for _, r := range value {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '-' || r == '_' || r == '.' {
			b.WriteRune(r)
		} else {
			b.WriteRune('_')
		}
	}
	return b.String()
}

// readCgroupCounters reads cpu.stat / memory.peak / io.stat from a per-exec
// cgroup.  Returns (cpuUserMicro, cpuSysMicro, memPeakBytes, readBytes,
// writeBytes, pids, ok).
func readCgroupCounters(path string) (cpuUserUs, cpuSysUs, memPeak, readBytes, writeBytes int64, pids int, ok bool) {
	cpuData, err := os.ReadFile(filepath.Join(path, "cpu.stat"))
	if err != nil {
		return 0, 0, 0, 0, 0, 0, false
	}
	for _, line := range strings.Split(string(cpuData), "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 {
			continue
		}
		switch fields[0] {
		case "usage_usec":
			cpuUserUs, _ = strconv.ParseInt(fields[1], 10, 64)
		case "user_usec":
			cpuUserUs, _ = strconv.ParseInt(fields[1], 10, 64)
		case "system_usec":
			cpuSysUs, _ = strconv.ParseInt(fields[1], 10, 64)
		}
	}
	if peak, err := os.ReadFile(filepath.Join(path, "memory.peak")); err == nil {
		memPeak, _ = strconv.ParseInt(strings.TrimSpace(string(peak)), 10, 64)
	}
	if ioData, err := os.ReadFile(filepath.Join(path, "io.stat")); err == nil {
		for _, line := range strings.Split(string(ioData), "\n") {
			fields := strings.Fields(line)
			for _, field := range fields {
				kv := strings.SplitN(field, "=", 2)
				if len(kv) != 2 {
					continue
				}
				switch kv[0] {
				case "rbytes":
					readBytes, _ = strconv.ParseInt(kv[1], 10, 64)
				case "wbytes":
					writeBytes, _ = strconv.ParseInt(kv[1], 10, 64)
				}
			}
		}
	}
	if pidData, err := os.ReadFile(filepath.Join(path, "pids.current")); err == nil {
		pids, _ = strconv.Atoi(strings.TrimSpace(string(pidData)))
	}
	ok = true
	return
}

// Finish stops sampling and returns the final merged stats.  When the
// per-execution cgroup was created, its exact counters replace the sampled
// process-tree values (cpu/mem/io), and the artifact is labelled cgroup-v2.
func (c *resourceCollector) Finish(tsEnd time.Time) resourceStats {
	close(c.done)
	c.mu.Lock()
	defer c.mu.Unlock()

	stats := resourceStats{
		Source:              "process-tree",
		MonitorSource:       "psutil-process-tree",
		AttributionSource:   "tool-bridge-pgid",
		TsEnd:               float64(tsEnd.UnixNano()) / 1e9,
		CPUUserSeconds:      c.cpuUserSeconds,
		CPUSystemSeconds:    c.cpuSysSeconds,
		RSSBeforeBytes:      c.rssBeforeKiB * 1024,
		RSSAfterBytes:       c.rssAfterKiB * 1024,
		RSSPeakBytes:        c.rssPeakKiB * 1024,
		ReadBytesDelta:      c.readBytes,
		WriteBytesDelta:     c.writeBytes,
		SamplingIntervalMS:  int64(c.intervalMS),
		SamplingPointCount:  c.pointCount,
		SamplingQuality:     "valid",
	}
	stats.CPUTimeSeconds = stats.CPUUserSeconds + stats.CPUSystemSeconds

	if c.cgroupOK && c.cgroupPath != "" {
		if userUs, sysUs, memPeak, rBytes, wBytes, pids, ok := readCgroupCounters(c.cgroupPath); ok {
			// Only trust the cgroup view when it actually observed the
			// execution (a created-but-empty cgroup reports zeros and must
			// not override the process-tree sampler).
			if userUs+sysUs > 0 || pids > 0 {
				stats.Source = "cgroup-v2"
				stats.MonitorSource = "cgroup-v2"
				stats.CPUUserSeconds = float64(userUs) / 1e6
				stats.CPUSystemSeconds = float64(sysUs) / 1e6
				stats.CPUTimeSeconds = stats.CPUUserSeconds + stats.CPUSystemSeconds
				if memPeak > 0 { // memory.peak may be absent on older guest kernels
					stats.RSSPeakBytes = memPeak
				}
				stats.ReadBytesDelta = rBytes
				stats.WriteBytesDelta = wBytes
				stats.PidCount = pids
			}
		}
		cleanupCgroup(c.cgroupPath)
	}
	return stats
}

// cleanupCgroup best-effort moves any lingering pids back to the parent and
// removes the per-exec cgroup.
func cleanupCgroup(path string) {
	parent := filepath.Dir(path)
	if data, err := os.ReadFile(filepath.Join(path, "cgroup.procs")); err == nil {
		for _, pid := range strings.Fields(string(data)) {
			_ = os.WriteFile(filepath.Join(parent, "cgroup.procs"), []byte(pid+"\n"), 0644)
		}
	}
	_ = os.Remove(path)
}

// writeResourceArtifact persists the per-execution resource artifact in the
// ClawTune cgroup_resource_v1 layout: <traceDir>/tool-resource/
// cgroup-resource-<execution_id>.json.
func writeResourceArtifact(execID string, stats resourceStats, traceDir string, started time.Time) {
	artifact := map[string]any{
		"schema":               "cgroup_resource_v1",
		"execution_id":         execID,
		"tool_call_id":         nil,
		"tool_name":            "",
		"source":               stats.Source,
		"monitor_source":       stats.MonitorSource,
		"attribution_source":   stats.AttributionSource,
		"ts_start":             float64(started.UnixNano()) / 1e9,
		"ts_end":               stats.TsEnd,
		"duration_ms":          stats.DurationMS,
		"cpu_time_s":           stats.CPUTimeSeconds,
		"cpu_utilization_avg_cores": cpuUtilization(stats.CPUTimeSeconds, stats.DurationMS),
		"memory_rss_before_bytes":   stats.RSSBeforeBytes,
		"memory_rss_after_bytes":    stats.RSSAfterBytes,
		"memory_rss_peak_bytes":     stats.RSSPeakBytes,
		"disk_read_bytes_delta":     stats.ReadBytesDelta,
		"disk_write_bytes_delta":    stats.WriteBytesDelta,
		"network_rx_bytes_delta":    nil,
		"network_tx_bytes_delta":    nil,
		"sampling_interval_ms":      stats.SamplingIntervalMS,
		"sampling_point_count":      stats.SamplingPointCount,
		"sampling_quality":          stats.SamplingQuality,
	}
	payload, err := json.Marshal(artifact)
	if err != nil {
		return
	}
	dir := filepath.Join(traceDir, "tool-resource")
	if err := os.MkdirAll(dir, 0700); err != nil {
		return
	}
	path := filepath.Join(dir, fmt.Sprintf("cgroup-resource-%s.json", sanitizeCgroupLeaf(execID)))
	_ = os.WriteFile(path, append(payload, '\n'), 0600)
}

func cpuUtilization(cpuSeconds float64, durationMS int64) float64 {
	if durationMS <= 0 {
		return 0
	}
	return cpuSeconds / (float64(durationMS) / 1000.0)
}
