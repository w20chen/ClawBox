package main

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

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
		}
	}))
	defer server.Close()
	p := &progressControl{URL: server.URL, Token: "private", Request: map[string]any{"execution_id": "e"}}
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
	}))
	defer server.Close()
	header, _ := json.Marshal(map[string]any{
		"v": 1, "execution_id": "progress-command",
		"progress_control": map[string]any{"url": server.URL, "token": "private"},
	})
	raw := clawboxExecEnvelopePrefix + "b64:" + base64.RawURLEncoding.EncodeToString(header) + "\nprintf actual-command"
	channel := &execTestChannel{}
	record := runCommand(channel, raw, t.TempDir(), time.Second, 1024)
	if record.ExitCode != 0 || channel.stdout.String() != "actual-command" {
		t.Fatalf("command changed: %+v", record)
	}
	if len(phases) != 2 || phases[0] != "start" || phases[1] != "end" { t.Fatal(phases) }
}
