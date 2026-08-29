// replay-guest-runtime is the minimal in-guest control loop for the
// direct-Firecracker experiment.  It deliberately uses the same SSH boundary
// as OpenClaw's production sandbox backend rather than a bespoke tool RPC.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
)

type action struct {
	Kind              string `json:"kind"`
	ActionID          string `json:"action_id"`
	Content           string `json:"content,omitempty"`
	RecordedLatencyMS int64  `json:"recorded_latency_ms,omitempty"`
	Command           string `json:"command,omitempty"`
	ExpectedExitCode  int    `json:"expected_exit_code,omitempty"`
}

type sshTarget struct {
	Address        string `json:"address"`
	User           string `json:"user"`
	IdentityFile   string `json:"identity_file"`
	KnownHostsFile string `json:"known_hosts_file"`
}

type config struct {
	SessionID    string    `json:"session_id"`
	InferenceURL string    `json:"inference_url"`
	StatePath    string    `json:"state_path"`
	SSH          sshTarget `json:"ssh"`
	Actions      []action  `json:"actions"`
}

type state struct {
	SessionID       string `json:"session_id"`
	NextAction      int    `json:"next_action"`
	InflightRequest string `json:"inflight_request,omitempty"`
}

type inferenceRequest struct {
	RequestID         string `json:"request_id"`
	SessionID         string `json:"session_id"`
	Content           string `json:"content"`
	RecordedLatencyMS int64  `json:"recorded_latency_ms"`
}

type inferenceStatus struct {
	RequestID string `json:"request_id"`
	Ready     bool   `json:"ready"`
	Result    string `json:"result,omitempty"`
}

func main() {
	configPath := flag.String("config", "/etc/clawbox/replay.json", "guest replay config")
	flag.Parse()
	var cfg config
	readJSON(*configPath, &cfg)
	if cfg.SessionID == "" || cfg.InferenceURL == "" || cfg.StatePath == "" || len(cfg.Actions) == 0 {
		fatalf("config requires session_id, inference_url, state_path, and actions")
	}
	current := loadState(cfg)
	for current.NextAction < len(cfg.Actions) {
		a := cfg.Actions[current.NextAction]
		switch a.Kind {
		case "llm":
			runLLM(cfg, &current, a)
		case "tool":
			if current.InflightRequest != "" {
				fatalf("tool %s reached with inflight request %s", a.ActionID, current.InflightRequest)
			}
			if code := runSSH(cfg.SSH, a.Command); code != a.ExpectedExitCode {
				fatalf("tool %s exit mismatch: expected %d, got %d", a.ActionID, a.ExpectedExitCode, code)
			}
			current.NextAction++
			saveState(cfg, current)
		default:
			fatalf("unsupported action kind %q", a.Kind)
		}
	}
	fmt.Printf("{\"ok\":true,\"session_id\":%q,\"actions\":%d}\n", cfg.SessionID, current.NextAction)
}

func runLLM(cfg config, current *state, a action) {
	requestID := a.ActionID
	if current.InflightRequest != "" && current.InflightRequest != requestID {
		fatalf("expected request %s but state holds %s", requestID, current.InflightRequest)
	}
	current.InflightRequest = requestID
	saveState(cfg, *current) // Persist before POST: retries are request-idempotent.
	request := inferenceRequest{requestID, cfg.SessionID, a.Content, a.RecordedLatencyMS}
	postJSON(cfg.InferenceURL+"/v1/replay/requests", request, nil)
	for {
		var status inferenceStatus
		getJSON(fmt.Sprintf("%s/v1/replay/requests/%s", cfg.InferenceURL, requestID), &status)
		if status.RequestID != requestID {
			fatalf("inference response request id mismatch: %q", status.RequestID)
		}
		if status.Ready {
			current.InflightRequest = ""
			current.NextAction++
			saveState(cfg, *current)
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func runSSH(target sshTarget, command string) int {
	if target.Address == "" || target.User == "" || target.IdentityFile == "" || target.KnownHostsFile == "" {
		fatalf("SSH target requires address, user, identity_file, and known_hosts_file")
	}
	key, err := os.ReadFile(target.IdentityFile)
	fatalIf(err, "read SSH identity")
	signer, err := ssh.ParsePrivateKey(key)
	fatalIf(err, "parse SSH identity")
	verifyHost, err := knownhosts.New(target.KnownHostsFile)
	fatalIf(err, "load SSH known_hosts")
	connection, err := net.DialTimeout("tcp", target.Address, 10*time.Second)
	fatalIf(err, "dial Tool SSH")
	defer connection.Close()
	clientConnection, channels, requests, err := ssh.NewClientConn(connection, target.Address, &ssh.ClientConfig{
		User: target.User, Auth: []ssh.AuthMethod{ssh.PublicKeys(signer)}, HostKeyCallback: verifyHost,
		Timeout: 10 * time.Second,
	})
	fatalIf(err, "SSH handshake")
	client := ssh.NewClient(clientConnection, channels, requests)
	defer client.Close()
	session, err := client.NewSession()
	fatalIf(err, "open SSH session")
	defer session.Close()
	var output bytes.Buffer
	session.Stdout, session.Stderr = &output, &output
	err = session.Run(command)
	if err == nil {
		return 0
	}
	var exitError *ssh.ExitError
	if errors.As(err, &exitError) {
		return exitError.ExitStatus()
	}
	fatalf("run Tool command: %v", err)
	return 127
}

func loadState(cfg config) state {
	current := state{SessionID: cfg.SessionID}
	data, err := os.ReadFile(cfg.StatePath)
	if os.IsNotExist(err) {
		return current
	}
	fatalIf(err, "read replay state")
	fatalIf(json.Unmarshal(data, &current), "decode replay state")
	if current.SessionID != cfg.SessionID || current.NextAction < 0 || current.NextAction > len(cfg.Actions) {
		fatalf("invalid replay state")
	}
	return current
}

func saveState(cfg config, current state) {
	fatalIf(os.MkdirAll(filepath.Dir(cfg.StatePath), 0700), "create replay state directory")
	encoded, err := json.Marshal(current)
	fatalIf(err, "encode replay state")
	temporary := cfg.StatePath + ".next"
	fatalIf(os.WriteFile(temporary, encoded, 0600), "write replay state")
	fatalIf(os.Rename(temporary, cfg.StatePath), "commit replay state")
}

func postJSON(url string, input any, output any) {
	payload, err := json.Marshal(input)
	fatalIf(err, "encode inference request")
	response, err := http.Post(url, "application/json", bytes.NewReader(payload))
	fatalIf(err, "submit inference request")
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		fatalf("inference POST returned %s", response.Status)
	}
	if output != nil {
		fatalIf(json.NewDecoder(response.Body).Decode(output), "decode inference response")
	}
}

func getJSON(url string, output any) {
	response, err := http.Get(url)
	fatalIf(err, "query inference request")
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		fatalf("inference GET returned %s", response.Status)
	}
	fatalIf(json.NewDecoder(response.Body).Decode(output), "decode inference status")
}

func readJSON(path string, output any) {
	data, err := os.ReadFile(path)
	fatalIf(err, "read config")
	fatalIf(json.Unmarshal(data, output), "decode config")
}
func fatalIf(err error, context string) {
	if err != nil {
		fatalf("%s: %v", context, err)
	}
}
func fatalf(format string, values ...any) {
	fmt.Fprintf(os.Stderr, "replay guest runtime: "+format+"\n", values...)
	os.Exit(1)
}

// Keep a deterministic identifier helper available for callers generating
// plans from arbitrary traces without leaking raw prompt content into state.
func requestID(sessionID, actionID string) string {
	sum := sha256.Sum256([]byte(sessionID + "\x00" + actionID))
	return hex.EncodeToString(sum[:])
}
