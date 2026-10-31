package main

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHostObservationRetriesIdenticalPayload(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/v1/kb/observe" || r.Header.Get("Authorization") != "Bearer pair-token" {
			t.Error("wrong route or binding")
		}
		var value map[string]any
		if json.NewDecoder(r.Body).Decode(&value) != nil || value["execution_id"] != "exec-a" {
			t.Error("payload changed")
		}
		if calls == 1 {
			w.WriteHeader(503)
			return
		}
		w.Write([]byte(`{"status":"duplicate","scope_generation":4}`))
	}))
	defer server.Close()
	t.Setenv("CLAWTUNE_HOST_KB_ENDPOINT", server.URL)
	t.Setenv("CLAWTUNE_HOST_KB_TOKEN", "pair-token")
	t.Setenv("CLAWTUNE_HOST_KB_TIMEOUT_SECONDS", "1")
	t.Setenv("CLAWBOX_KB_OBSERVATION_RETRIES", "2")
	status, generation, err := postKnowledge(map[string]any{"execution_id": "exec-a"})
	if err != nil || status != "duplicate" || generation != 4 || calls != 2 {
		t.Fatalf("%s %d %v calls=%d", status, generation, err, calls)
	}
}

func TestPredictionReceiptSurvivesWrappedEnvelope(t *testing.T) {
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"v":1,"prediction_receipt":"opaque","tool_name":"terminal_exec","call_prediction":{"targets":{}}}`))
	command := "cd /testbed; " + clawboxExecEnvelopePrefix + "b64:" + header + "\nsleep 1"
	if predictionReceipt(command) != "opaque" || envelopeField(command, "tool_name") != "terminal_exec" {
		t.Fatal("lost envelope binding")
	}
}
