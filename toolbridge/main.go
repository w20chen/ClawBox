package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/crypto/ssh"
)

const version = "0.1.0"

type executionLog struct {
	Timestamp       string `json:"timestamp"`
	CellID          string `json:"cell_id"`
	TaskID          string `json:"task_id"`
	ExecutionID     string `json:"execution_id"`
	ExecutionSource string `json:"execution_source"`
	CommandSHA256   string `json:"command_sha256"`
	CommandBytes    int    `json:"command_bytes"`
	DurationMS      int64  `json:"duration_ms"`
	ExitCode        int    `json:"exit_code"`
	TimedOut        bool   `json:"timed_out"`
	StdoutBytes     int64  `json:"stdout_bytes"`
	StderrBytes     int64  `json:"stderr_bytes"`
	OutputTruncated bool   `json:"output_truncated"`
	UserCPUMS       int64  `json:"user_cpu_ms"`
	SystemCPUMS     int64  `json:"system_cpu_ms"`
	MaxRSSKiB       int64  `json:"max_rss_kib"`
}

var executionLogMu sync.Mutex

func persistExecutionLog(record executionLog) {
	encoded, _ := json.Marshal(record)
	log.Print(string(encoded))
	path := os.Getenv("TOOL_BRIDGE_LOG_PATH")
	if path == "" {
		path = "/testbed/.clawbox/tool-bridge.jsonl"
	}
	executionLogMu.Lock()
	defer executionLogMu.Unlock()
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		log.Printf("tool bridge audit directory failed: %v", err)
		return
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		log.Printf("tool bridge audit open failed: %v", err)
		return
	}
	defer file.Close()
	_, _ = file.Write(append(encoded, '\n'))
}

type limitedStream struct {
	mu        sync.Mutex
	dst       io.Writer
	limit     int64
	total     int64
	written   int64
	truncated bool
}

func (w *limitedStream) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.total += int64(len(p))
	remaining := w.limit - w.written
	if remaining <= 0 {
		w.truncated = true
		return len(p), nil
	}
	part := p
	if int64(len(part)) > remaining {
		part = part[:remaining]
		w.truncated = true
	}
	n, err := w.dst.Write(part)
	w.written += int64(n)
	if err != nil {
		return n, err
	}
	return len(p), nil
}

func envInt(name string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed <= 0 {
		log.Fatalf("invalid %s=%q", name, value)
	}
	return parsed
}

func loadServerConfig(hostKeyPath, authorizedKeysPath string) *ssh.ServerConfig {
	hostPEM, err := os.ReadFile(hostKeyPath)
	if err != nil {
		log.Fatalf("read host key: %v", err)
	}
	signer, err := ssh.ParsePrivateKey(hostPEM)
	if err != nil {
		log.Fatalf("parse host key: %v", err)
	}
	authorized, err := os.ReadFile(authorizedKeysPath)
	if err != nil {
		log.Fatalf("read authorized key: %v", err)
	}
	key, _, _, _, err := ssh.ParseAuthorizedKey(authorized)
	if err != nil {
		log.Fatalf("parse authorized key: %v", err)
	}
	wanted := ssh.FingerprintSHA256(key)
	config := &ssh.ServerConfig{
		NoClientAuth: false,
		PublicKeyCallback: func(metadata ssh.ConnMetadata, candidate ssh.PublicKey) (*ssh.Permissions, error) {
			if metadata.User() != "executor" {
				return nil, errors.New("SSH user must be executor")
			}
			if ssh.FingerprintSHA256(candidate) != wanted {
				return nil, errors.New("public key is not authorized for this task")
			}
			return &ssh.Permissions{Extensions: map[string]string{"fingerprint": wanted}}, nil
		},
	}
	config.AddHostKey(signer)
	return config
}

func decodeSSHString(payload []byte) (string, error) {
	if len(payload) < 4 {
		return "", errors.New("short SSH string")
	}
	size := int(binary.BigEndian.Uint32(payload[:4]))
	if size < 0 || size > len(payload)-4 {
		return "", errors.New("invalid SSH string length")
	}
	return string(payload[4 : 4+size]), nil
}

func randomID() string {
	value := make([]byte, 16)
	if _, err := rand.Read(value); err != nil {
		return fmt.Sprintf("fallback-%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(value)
}

func durationMS(value time.Duration) int64 { return value.Milliseconds() }

// clawboxExecEnvelopePrefix is a single-line marker the ClawTune plugin
// prepends to an exec command (in hook-only mode) so the tool bridge adopts
// the runtime-generated execution_id instead of minting its own.  This gives
// an exact join key between the ClawTune span and the bridge execution record
// (no time-window heuristic).
const clawboxExecEnvelopePrefix = "__CBX_EXEC_1__"

type execEnvelope struct {
	Version     int    `json:"v"`
	ExecutionID string `json:"execution_id"`
}

// parseExecEnvelope splits an optional runtime envelope from the actual shell
// command.  A command that does not carry the envelope prefix is returned
// unchanged with ok=false so the bridge stays fully backward compatible with
// raw commands.  Any malformed envelope also degrades to the raw command.
func parseExecEnvelope(command string) (payload string, executionID string, ok bool) {
	marker := strings.Index(command, clawboxExecEnvelopePrefix)
	if marker < 0 {
		return command, "", false
	}
	rest := command[marker+len(clawboxExecEnvelopePrefix):]
	newline := strings.IndexByte(rest, '\n')
	if newline < 0 {
		return command, "", false
	}
	header := strings.TrimSpace(rest[:newline])
	payload = command[:marker] + rest[newline+1:]
	executionID = header
	if strings.HasPrefix(header, "{") {
		var envelope execEnvelope
		if err := json.Unmarshal([]byte(header), &envelope); err != nil || envelope.Version != 1 {
			return command, "", false
		}
		executionID = envelope.ExecutionID
	}
	if !validEnvelopeExecutionID(executionID) {
		return command, "", false
	}
	return payload, executionID, true
}

func validEnvelopeExecutionID(value string) bool {
	if value == "" || len(value) > 128 {
		return false
	}
	for _, char := range value {
		if (char >= 'a' && char <= 'z') || (char >= 'A' && char <= 'Z') ||
			(char >= '0' && char <= '9') || strings.ContainsRune("-_.:", char) {
			continue
		}
		return false
	}
	return true
}

func runCommand(channel ssh.Channel, rawCommand, workdir string, timeout time.Duration, outputLimit int64) executionLog {
	started := time.Now()
	command, envelopeExecutionID, enveloped := parseExecEnvelope(rawCommand)
	executionID := envelopeExecutionID
	if !enveloped || executionID == "" {
		executionID = randomID()
	}
	executionSource := "bridge-local"
	if enveloped {
		executionSource = "runtime-envelope"
	}
	digest := sha256.Sum256([]byte(command))
	record := executionLog{
		Timestamp:     started.UTC().Format(time.RFC3339Nano),
		CellID:        os.Getenv("CELL_ID"),
		TaskID:        os.Getenv("TASK_ID"),
		ExecutionID:   executionID,
		ExecutionSource: executionSource,
		CommandSHA256: hex.EncodeToString(digest[:]),
		CommandBytes:  len(command),
		ExitCode:      127,
	}

	cmd := exec.Command("/bin/sh", "-lc", command)
	cmd.Dir = workdir
	cmd.Env = append(os.Environ(), "CLAWBOX_TOOL_EXECUTION_ID="+executionID)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Stdin = channel
	stdout := &limitedStream{dst: channel, limit: outputLimit}
	stderr := &limitedStream{dst: channel.Stderr(), limit: outputLimit}
	cmd.Stdout = stdout
	cmd.Stderr = stderr

	var collector *resourceCollector
	err := cmd.Start()
	if err == nil {
		// Process-tree (and best-effort per-exec cgroup) collection.  The
		// shell is the pgid leader (Setpgid), so the whole tool tree shares
		// cmd.Process.Pid as its process-group id.
		collector = startResourceCollector(cmd.Process.Pid, executionID, resourceTraceDir(), 100)
		done := make(chan error, 1)
		go func() { done <- cmd.Wait() }()
		timer := time.NewTimer(timeout)
		select {
		case err = <-done:
			timer.Stop()
		case <-timer.C:
			record.TimedOut = true
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
			select {
			case err = <-done:
			case <-time.After(5 * time.Second):
				_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
				err = <-done
			}
		}
	}
	ended := time.Now()
	record.DurationMS = durationMS(ended.Sub(started))
	record.StdoutBytes = stdout.total
	record.StderrBytes = stderr.total
	record.OutputTruncated = stdout.truncated || stderr.truncated
	if record.TimedOut {
		record.ExitCode = 124
	} else if err == nil {
		record.ExitCode = 0
	} else {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			record.ExitCode = exitErr.ExitCode()
		}
	}
	// Prefer the real per-execution numbers from the process-tree/cgroup
	// collector over the direct-child (shell) rusage that the old code read.
	if collector != nil {
		stats := collector.Finish(ended)
		stats.DurationMS = record.DurationMS
		record.UserCPUMS = int64(stats.CPUUserSeconds * 1000)
		record.SystemCPUMS = int64(stats.CPUSystemSeconds * 1000)
		record.MaxRSSKiB = stats.RSSPeakBytes / 1024
		writeResourceArtifact(executionID, stats, resourceTraceDir(), started)
	} else if cmd.ProcessState != nil {
		record.UserCPUMS = durationMS(cmd.ProcessState.UserTime())
		record.SystemCPUMS = durationMS(cmd.ProcessState.SystemTime())
		if usage, ok := cmd.ProcessState.SysUsage().(*syscall.Rusage); ok {
			record.MaxRSSKiB = usage.Maxrss
		}
	}
	return record
}

func handleSession(channel ssh.Channel, requests <-chan *ssh.Request, workdir string, timeout time.Duration, outputLimit int64, semaphore chan struct{}) {
	defer channel.Close()
	defer func() { <-semaphore }()
	for request := range requests {
		if request.Type != "exec" {
			_ = request.Reply(false, nil)
			continue
		}
		command, err := decodeSSHString(request.Payload)
		if err != nil || strings.TrimSpace(command) == "" {
			_ = request.Reply(false, nil)
			return
		}
		_ = request.Reply(true, nil)
		record := runCommand(channel, command, workdir, timeout, outputLimit)
		persistExecutionLog(record)
		_, _ = channel.SendRequest("exit-status", false, ssh.Marshal(struct{ Status uint32 }{uint32(record.ExitCode)}))
		return
	}
}

func handleConnection(raw net.Conn, config *ssh.ServerConfig, workdir string, timeout time.Duration, outputLimit int64, semaphore chan struct{}) {
	defer raw.Close()
	_ = raw.SetDeadline(time.Now().Add(30 * time.Second))
	connection, channels, requests, err := ssh.NewServerConn(raw, config)
	if err != nil {
		log.Printf("ssh handshake failed remote=%s error=%q", raw.RemoteAddr(), err)
		return
	}
	_ = raw.SetDeadline(time.Time{})
	defer connection.Close()
	go ssh.DiscardRequests(requests)
	for incoming := range channels {
		if incoming.ChannelType() != "session" {
			_ = incoming.Reject(ssh.UnknownChannelType, "only session channels are supported")
			continue
		}
		channel, channelRequests, err := incoming.Accept()
		if err != nil {
			continue
		}
		select {
		case semaphore <- struct{}{}:
			go handleSession(channel, channelRequests, workdir, timeout, outputLimit, semaphore)
		default:
			_ = channel.Close()
		}
	}
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--self-test" {
		fmt.Printf("{\"name\":\"clawbox-tool-bridge\",\"version\":%q,\"arch\":%q}\n", version, runtime.GOARCH)
		if runtime.GOARCH != "arm64" {
			os.Exit(1)
		}
		return
	}
	address := os.Getenv("TOOL_BRIDGE_LISTEN")
	if address == "" {
		address = "0.0.0.0:2222"
	}
	workdir := os.Getenv("TOOL_BRIDGE_WORKDIR")
	if workdir == "" {
		workdir = "/testbed"
	}
	if info, err := os.Stat(workdir); err != nil || !info.IsDir() {
		log.Fatalf("task workdir is unavailable: %s", workdir)
	}
	hostKey := os.Getenv("TOOL_BRIDGE_HOST_KEY")
	if hostKey == "" {
		hostKey = "/var/run/secrets/tool-ssh/ssh_host_ed25519_key"
	}
	authorizedKey := os.Getenv("TOOL_BRIDGE_AUTHORIZED_KEY")
	if authorizedKey == "" {
		authorizedKey = "/var/run/secrets/tool-ssh/id_ed25519.pub"
	}
	config := loadServerConfig(hostKey, authorizedKey)
	timeout := time.Duration(envInt("TOOL_EXEC_TIMEOUT_SECONDS", 300)) * time.Second
	outputLimit := int64(envInt("TOOL_OUTPUT_LIMIT_BYTES", 4*1024*1024))
	semaphore := make(chan struct{}, envInt("TOOL_MAX_CONCURRENCY", 4))
	listener, err := net.Listen("tcp", address)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("tool bridge ready address=%s workdir=%s arch=%s", address, workdir, runtime.GOARCH)
	for {
		connection, err := listener.Accept()
		if err != nil {
			log.Printf("accept failed: %v", err)
			continue
		}
		go handleConnection(connection, config, workdir, timeout, outputLimit, semaphore)
	}
}
