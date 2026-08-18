package main

import "testing"

func TestParseExecEnvelope_PlainCommand(t *testing.T) {
	payload, executionID, ok := parseExecEnvelope("echo hello")
	if !ok {
		t.Fatal("plain command should report ok=true so the raw command is used")
	}
	if payload != "echo hello" {
		t.Fatalf("payload mismatch: %q", payload)
	}
	if executionID != "" {
		t.Fatalf("expected empty execution_id, got %q", executionID)
	}
}

func TestParseExecEnvelope_Valid(t *testing.T) {
	raw := "__CBX_EXEC_1__{\"v\":1,\"execution_id\":\"exec-1234-5678\"}\npytest -q"
	payload, executionID, ok := parseExecEnvelope(raw)
	if !ok {
		t.Fatal("expected a valid envelope")
	}
	if payload != "pytest -q" {
		t.Fatalf("payload mismatch: %q", payload)
	}
	if executionID != "exec-1234-5678" {
		t.Fatalf("execution_id mismatch: %q", executionID)
	}
}

func TestParseExecEnvelope_PayloadMayContainNewlines(t *testing.T) {
	raw := "__CBX_EXEC_1__{\"v\":1,\"execution_id\":\"exec-abc\"}\nprintf 'a\nb\n'"
	payload, executionID, ok := parseExecEnvelope(raw)
	if !ok {
		t.Fatal("expected a valid envelope")
	}
	if payload != "printf 'a\nb\n'" {
		t.Fatalf("payload mismatch: %q", payload)
	}
	if executionID != "exec-abc" {
		t.Fatalf("execution_id mismatch: %q", executionID)
	}
}

func TestParseExecEnvelope_MalformedJSON(t *testing.T) {
	raw := "__CBX_EXEC_1__{not-json}\necho hi"
	payload, _, ok := parseExecEnvelope(raw)
	if ok {
		t.Fatal("malformed JSON should degrade to raw command (ok=false)")
	}
	if payload != raw {
		t.Fatalf("expected raw command unchanged, got %q", payload)
	}
}

func TestParseExecEnvelope_WrongVersion(t *testing.T) {
	raw := "__CBX_EXEC_1__{\"v\":2,\"execution_id\":\"exec-x\"}\necho hi"
	payload, _, ok := parseExecEnvelope(raw)
	if ok {
		t.Fatal("wrong envelope version should degrade to raw command")
	}
	if payload != raw {
		t.Fatalf("expected raw command unchanged, got %q", payload)
	}
}

func TestParseExecEnvelope_EmptyExecutionID(t *testing.T) {
	raw := "__CBX_EXEC_1__{\"v\":1,\"execution_id\":\"\"}\necho hi"
	payload, _, ok := parseExecEnvelope(raw)
	if ok {
		t.Fatal("empty execution_id should degrade to raw command")
	}
	if payload != raw {
		t.Fatalf("expected raw command unchanged, got %q", payload)
	}
}

func TestParseExecEnvelope_NoNewline(t *testing.T) {
	raw := "__CBX_EXEC_1__{\"v\":1,\"execution_id\":\"exec-x\"}"
	payload, _, ok := parseExecEnvelope(raw)
	if ok {
		t.Fatal("envelope without a payload line should degrade to raw command")
	}
	if payload != raw {
		t.Fatalf("expected raw command unchanged, got %q", payload)
	}
}
