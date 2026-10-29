package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

func TestStartAckDoesNotDelayProcessLifetime(t *testing.T) {
	for _, test := range []struct {
		name                  string
		command               string
		timeout               time.Duration
		ackDelay              time.Duration
		maxExecutionMS        float64
		wantTimeout           bool
		wantCancelled         bool
		rejectStart           bool
		runningUntilRejection bool
	}{
		{name: "completed", command: "sleep .05", timeout: 2 * time.Second,
			ackDelay: 450 * time.Millisecond, maxExecutionMS: 300},
		{name: "timeout", command: "sleep 1.5; touch too-late", timeout: time.Second,
			ackDelay: 2200 * time.Millisecond, maxExecutionMS: 1700, wantTimeout: true},
		{name: "cancelled", command: "sleep 30", timeout: 2 * time.Second,
			ackDelay: 450 * time.Millisecond, maxExecutionMS: 300, wantCancelled: true},
		{name: "policy-rejected", command: "sleep .05", timeout: 2 * time.Second,
			ackDelay: 450 * time.Millisecond, maxExecutionMS: 300, rejectStart: true},
		{name: "policy-rejected-running", command: "sleep 30", timeout: 2 * time.Second,
			ackDelay: 450 * time.Millisecond, maxExecutionMS: 900, rejectStart: true, runningUntilRejection: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			workdir := t.TempDir()
			t.Setenv("TOOL_BRIDGE_LOG_PATH", filepath.Join(workdir, "bridge.jsonl"))
			var phaseMu sync.Mutex
			var phases []string
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				_ = json.NewDecoder(r.Body).Decode(&body)
				phase := body["phase"].(string)
				phaseMu.Lock()
				phases = append(phases, phase)
				phaseMu.Unlock()
				if phase == "start" {
					if test.wantCancelled {
						time.Sleep(50 * time.Millisecond)
						if !cancelExecution(body["execution_id"].(string)) {
							t.Error("execution was unavailable for cancellation")
						}
						time.Sleep(test.ackDelay - 50*time.Millisecond)
					} else {
						time.Sleep(test.ackDelay)
					}
					if test.rejectStart {
						w.WriteHeader(http.StatusForbidden)
						return
					}
				}
				acknowledgeProgress(w, body)
			}))
			defer server.Close()
			executionID := "delayed-start-" + test.name
			header, _ := json.Marshal(map[string]any{
				"v": 1, "execution_id": executionID,
				"progress_control": map[string]any{"url": server.URL, "token": "private", "timeout_seconds": 9,
					"request": map[string]any{"session_id": "s", "execution_id": executionID}},
			})
			payload := "echo $$ > command-pid; printf once >> payload-runs; " + test.command
			raw := clawboxExecEnvelopePrefix + "b64:" + base64.RawURLEncoding.EncodeToString(header) + "\n" + payload
			channel := &stdinTestChannel{reader: bytes.NewReader(nil)}
			began := time.Now()
			record := runCommand(channel, raw, workdir, test.timeout, 1024)
			elapsed := time.Since(began)
			if elapsed < test.ackDelay {
				t.Fatalf("start acknowledgement was not awaited: %s", elapsed)
			}
			if record.TimedOut != test.wantTimeout {
				t.Errorf("timeout did not run during start acknowledgement: %+v", record)
			}
			wantExit := 0
			if test.wantTimeout {
				wantExit = 124
			}
			if test.wantCancelled {
				wantExit = 130
			}
			if test.rejectStart {
				wantExit = 125
				if !strings.Contains(record.TelemetryError, "progress start status 403") {
					t.Errorf("policy error was hidden: %+v", record)
				}
			}
			if record.ExitCode != wantExit || record.Cancelled != test.wantCancelled {
				t.Errorf("unexpected execution result: %+v", record)
			}
			if !test.runningUntilRejection && record.DurationMS >= elapsed.Milliseconds()-200 {
				t.Errorf("process duration includes start acknowledgement: %d ms", record.DurationMS)
			}
			artifact, err := os.ReadFile(filepath.Join(workdir, "tool-resource", "cgroup-resource-"+executionID+".json"))
			if err != nil {
				t.Fatal(err)
			}
			var resource map[string]any
			if err := json.Unmarshal(artifact, &resource); err != nil {
				t.Fatal(err)
			}
			if duration := resource["duration_ms"].(float64); duration >= test.maxExecutionMS {
				t.Errorf("collector end includes start acknowledgement: %.0f ms", duration)
			}
			if data, err := os.ReadFile(filepath.Join(workdir, "payload-runs")); err != nil || string(data) != "once" {
				t.Errorf("payload did not execute exactly once: %q, %v", data, err)
			}
			if _, err := os.Stat(filepath.Join(workdir, "too-late")); !os.IsNotExist(err) {
				t.Errorf("payload continued past its timeout while start acknowledgement blocked: %v", err)
			}
			pidData, err := os.ReadFile(filepath.Join(workdir, "command-pid"))
			if err != nil {
				t.Fatal(err)
			}
			pid, err := strconv.Atoi(strings.TrimSpace(string(pidData)))
			if err != nil {
				t.Fatal(err)
			}
			if err := syscall.Kill(pid, 0); err != syscall.ESRCH {
				t.Errorf("command was not reaped: pid=%d err=%v", pid, err)
			}
			if cancelExecution(executionID) {
				t.Error("completed execution remains registered")
			}
			phaseMu.Lock()
			defer phaseMu.Unlock()
			if strings.Join(phases, ",") != "start,end" {
				t.Errorf("progress order changed: %v", phases)
			}
		})
	}
}
