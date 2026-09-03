package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

const guestCollectorProtocolVersion = 1

type guestCollectorResponse struct {
	OK                 bool           `json:"ok"`
	Version            int            `json:"v"`
	State              string         `json:"state"`
	Error              string         `json:"error"`
	ArtifactPath       string         `json:"artifact_path"`
	EligibleForKB      bool           `json:"eligible_for_kb"`
	TelemetryQuality   string         `json:"telemetry_quality"`
	CollectionValidity string         `json:"collection_validity"`
	Cleanup            string         `json:"cleanup"`
	LossTotal          int64          `json:"loss_total"`
	BPFRuntime         map[string]any `json:"bpf_runtime"`
}

type guestCollectorAPI interface {
	Begin(executionID, command, cgroupPath string, trustedRootPID int, repo string) (guestCollectorResponse, error)
	Finish(executionID string, returnCode int) (guestCollectorResponse, error)
	Abort(executionID string) error
}

type guestCollectorClient struct {
	socket  string
	token   string
	timeout time.Duration
}

func (c *guestCollectorClient) request(values map[string]any) (guestCollectorResponse, error) {
	values["v"] = guestCollectorProtocolVersion
	values["token"] = c.token
	connection, err := net.DialTimeout("unix", c.socket, c.timeout)
	if err != nil {
		return guestCollectorResponse{}, err
	}
	defer connection.Close()
	_ = connection.SetDeadline(time.Now().Add(c.timeout))
	if err := json.NewEncoder(connection).Encode(values); err != nil {
		return guestCollectorResponse{}, err
	}
	line, err := bufio.NewReader(io.LimitReader(connection, 256*1024+1)).ReadBytes('\n')
	if err != nil {
		return guestCollectorResponse{}, err
	}
	if len(line) > 256*1024 {
		return guestCollectorResponse{}, errors.New("guest collector response too large")
	}
	var response guestCollectorResponse
	if err := json.Unmarshal(line, &response); err != nil {
		return guestCollectorResponse{}, err
	}
	if !response.OK {
		if response.Error == "" {
			response.Error = "guest collector rejected request"
		}
		return response, errors.New(response.Error)
	}
	if response.Version != guestCollectorProtocolVersion {
		return response, fmt.Errorf("guest collector protocol version %d", response.Version)
	}
	return response, nil
}

func (c *guestCollectorClient) Begin(executionID, command, cgroupPath string, trustedRootPID int, repo string) (guestCollectorResponse, error) {
	return c.request(map[string]any{
		"op":               "begin",
		"execution_id":     executionID,
		"command":          command,
		"cgroup_path":      cgroupPath,
		"trusted_root_pid": trustedRootPID,
		"repo":             repo,
	})
}

func (c *guestCollectorClient) Finish(executionID string, returnCode int) (guestCollectorResponse, error) {
	return c.request(map[string]any{
		"op":           "finish",
		"execution_id": executionID,
		"return_code":  returnCode,
	})
}

func (c *guestCollectorClient) Abort(executionID string) error {
	_, err := c.request(map[string]any{"op": "abort", "execution_id": executionID})
	return err
}

type guestCollectorProcess struct {
	client *guestCollectorClient
	cmd    *exec.Cmd
	done   chan error
	once   sync.Once
}

func randomCollectorToken() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw), nil
}

// ensureTracefs mounts the Cube guest tracefs before BCC starts. The minimal
// guest kernel supports tracefs, but does not mount it by default. BCC can
// compile its tracepoint program in that state and then fails at attach time
// because the tracepoint id files do not exist. Tool pods carry SYS_ADMIN
// inside their VM specifically for guest-local cgroup/eBPF setup.
func ensureTracefs() error {
	const tracefs = "/sys/kernel/tracing"
	eventID := filepath.Join(tracefs, "events/sched/sched_process_exit/id")
	if _, err := os.Stat(eventID); err == nil {
		return nil
	}
	if err := os.MkdirAll(tracefs, 0755); err != nil {
		return fmt.Errorf("create tracefs mountpoint: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	output, mountErr := exec.CommandContext(
		ctx, "mount", "-t", "tracefs", "tracefs", tracefs,
	).CombinedOutput()
	if _, err := os.Stat(eventID); err == nil {
		return nil
	}
	if mountErr != nil {
		return fmt.Errorf("mount tracefs: %w: %s", mountErr, strings.TrimSpace(string(output)))
	}
	return fmt.Errorf("tracefs mounted without sched_process_exit tracepoint: %s", eventID)
}

func startGuestCollectorProcess() (*guestCollectorProcess, error) {
	helper := os.Getenv("CLAWTUNE_GUEST_COLLECTOR_HELPER")
	if helper == "" {
		return nil, nil
	}
	if err := ensureTracefs(); err != nil {
		return nil, err
	}
	python := os.Getenv("CLAWTUNE_GUEST_COLLECTOR_PYTHON")
	if python == "" {
		python = "/opt/clawtune/venv/bin/python"
	}
	socket := os.Getenv("CLAWTUNE_GUEST_COLLECTOR_SOCKET")
	if socket == "" {
		socket = "/run/clawtune/guest-collector.sock"
	}
	artifactRoot := os.Getenv("CLAWTUNE_GUEST_ARTIFACT_ROOT")
	if artifactRoot == "" {
		artifactRoot = resourceTraceDir() + "/tool-resource"
	}
	token, err := randomCollectorToken()
	if err != nil {
		return nil, err
	}
	cmd := exec.Command(python, helper,
		"--socket", socket,
		"--artifact-root", artifactRoot,
		"--max-active", strconv.Itoa(envInt("TOOL_MAX_CONCURRENCY", 4)),
	)
	cmd.Env = append(os.Environ(), "CLAWTUNE_GUEST_COLLECTOR_TOKEN="+token)
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	process := &guestCollectorProcess{
		client: &guestCollectorClient{socket: socket, token: token, timeout: 15 * time.Second},
		cmd:    cmd,
		done:   make(chan error, 1),
	}
	go func() { process.done <- cmd.Wait() }()
	deadline := time.Now().Add(time.Duration(envInt("CLAWTUNE_GUEST_STARTUP_TIMEOUT_SECONDS", 30)) * time.Second)
	for time.Now().Before(deadline) {
		response, healthErr := process.client.request(map[string]any{"op": "health"})
		if healthErr == nil && response.State == "ready" {
			return process, nil
		}
		select {
		case processErr := <-process.done:
			return nil, fmt.Errorf("guest collector helper exited: %w", processErr)
		default:
		}
		time.Sleep(100 * time.Millisecond)
	}
	process.Stop()
	return nil, errors.New("guest collector helper did not become ready")
}

func (p *guestCollectorProcess) Stop() {
	if p == nil {
		return
	}
	p.once.Do(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		connection, err := (&net.Dialer{}).DialContext(ctx, "unix", p.client.socket)
		if err == nil {
			_ = json.NewEncoder(connection).Encode(map[string]any{
				"v": guestCollectorProtocolVersion, "token": p.client.token, "op": "shutdown",
			})
			_ = connection.Close()
		}
		select {
		case <-p.done:
		case <-time.After(3 * time.Second):
			_ = p.cmd.Process.Kill()
			<-p.done
		}
	})
}
