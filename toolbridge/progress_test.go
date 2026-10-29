package main

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func acknowledgeProgress(w http.ResponseWriter, body map[string]any) {
	_ = json.NewEncoder(w).Encode(map[string]any{
		"status": "STARTED", "session_id": body["session_id"],
		"execution_id": body["execution_id"],
	})
}

func TestProgressEnvelopePreservesCommand(t *testing.T) {
	header, _ := json.Marshal(map[string]any{
		"v": 1, "execution_id": "exec-1",
		"progress_control": map[string]any{"url": "http://host", "token": "private"},
	})
	raw := clawboxExecEnvelopePrefix + "b64:" + base64.RawURLEncoding.EncodeToString(header) + "\nprintf test"
	command, id, profile, ok := parseExecEnvelope(raw)
	if !ok || command != "printf test" || profile != command || id != "exec-1" {
		t.Fatal("progress metadata changed command identity")
	}
	progress := parseProgressControl(raw)
	if progress == nil || progress.Token != "private" {
		t.Fatal("missing progress callback")
	}
}

func TestProgressDeliveryAndFailure(t *testing.T) {
	var phases []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer private" || r.URL.Path != "/v1/tool/start" {
			t.Error("wrong authenticated endpoint")
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		phases = append(phases, body["phase"].(string))
		if body["phase"] == "end" {
			w.WriteHeader(http.StatusConflict)
			return
		}
		acknowledgeProgress(w, body)
	}))
	defer server.Close()
	p := &progressControl{URL: server.URL, Token: "private", TimeoutSeconds: 1, Request: map[string]any{"session_id": "s", "execution_id": "e"}}
	if err := p.report("start"); err != nil {
		t.Fatal(err)
	}
	if err := p.report("end"); err == nil {
		t.Fatal("lost end event was hidden")
	}
	if len(phases) != 2 || phases[0] != "start" || phases[1] != "end" {
		t.Fatal(phases)
	}
}

func TestCommandReportsActualBoundaries(t *testing.T) {
	var phases []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		phases = append(phases, body["phase"].(string))
		acknowledgeProgress(w, body)
	}))
	defer server.Close()
	header, _ := json.Marshal(map[string]any{
		"v": 1, "execution_id": "progress-command",
		"progress_control": map[string]any{"url": server.URL, "token": "private", "timeout_seconds": 1,
			"request": map[string]any{"session_id": "s", "execution_id": "progress-command"}},
	})
	raw := clawboxExecEnvelopePrefix + "b64:" + base64.RawURLEncoding.EncodeToString(header) + "\nprintf actual-command"
	channel := &execTestChannel{}
	record := runCommand(channel, raw, t.TempDir(), time.Second, 1024)
	if record.ExitCode != 0 || channel.stdout.String() != "actual-command" {
		t.Fatalf("command changed: %+v", record)
	}
	if len(phases) != 2 || phases[0] != "start" || phases[1] != "end" {
		t.Fatal(phases)
	}
}

func TestProgressRetriesLostAcknowledgementAndReusesConnection(t *testing.T) {
	var attempts atomic.Int32
	var boundaries atomic.Int32
	seen := make(map[string]bool)
	var addresses []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		phase := body["phase"].(string)
		if !seen[phase] {
			seen[phase] = true
			boundaries.Add(1)
		}
		if attempts.Add(1) == 1 {
			// The callback committed, but its acknowledgement was lost.
			connection, _, _ := w.(http.Hijacker).Hijack()
			connection.Close()
			return
		}
		addresses = append(addresses, r.RemoteAddr)
		acknowledgeProgress(w, body)
	}))
	defer server.Close()
	p := &progressControl{URL: server.URL, Token: "private", TimeoutSeconds: 1,
		Request: map[string]any{"session_id": "s", "execution_id": "e"}}
	if err := p.report("start"); err != nil {
		t.Fatal(err)
	}
	if err := p.report("end"); err != nil {
		t.Fatal(err)
	}
	if attempts.Load() != 3 || boundaries.Load() != 2 {
		t.Fatal(attempts.Load(), boundaries.Load())
	}
	if len(addresses) != 2 || addresses[0] != addresses[1] {
		t.Fatalf("connection not reused: %v", addresses)
	}
}

func TestProgressDoesNotAcceptWrongAcknowledgement(t *testing.T) {
	var attempts atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		attempts.Add(1)
		_, _ = w.Write([]byte(`{"status":"STARTED","session_id":"s","execution_id":"other"}`))
	}))
	defer server.Close()
	p := &progressControl{URL: server.URL, Token: "private", TimeoutSeconds: 1,
		Request: map[string]any{"session_id": "s", "execution_id": "e"}}
	if err := p.report("start"); err == nil || !strings.Contains(err.Error(), "does not match") {
		t.Fatal(err)
	}
	if attempts.Load() != 1 {
		t.Fatal("protocol failure was retried")
	}
}

func TestProgressDeadlineIsBoundedAndDoesNotLeakCredentials(t *testing.T) {
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-release
	}))
	defer server.Close()
	defer close(release)
	p := &progressControl{URL: server.URL, Token: "private-secret", TimeoutSeconds: 0.09,
		Request: map[string]any{"session_id": "s", "execution_id": "e"}}
	began := time.Now()
	err := p.report("end")
	if err == nil || !strings.Contains(err.Error(), "timeout") || strings.Contains(err.Error(), "private-secret") || strings.Contains(err.Error(), server.URL) {
		t.Fatal(err)
	}
	if time.Since(began) > time.Second {
		t.Fatal("progress exceeded bounded deadline")
	}
}
