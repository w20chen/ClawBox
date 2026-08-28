package main

import (
	"bytes"
	"io"
	"testing"
	"time"
)

func TestParseExecEnvelope_PlainCommand(t *testing.T) {
	payload, executionID, ok := parseExecEnvelope("echo hello")
	if ok {
		t.Fatal("plain command should not report an envelope")
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

func TestParseExecEnvelope_ShellSafeToken(t *testing.T) {
	raw := "__CBX_EXEC_1__exec-1234-5678\npytest -q"
	payload, executionID, ok := parseExecEnvelope(raw)
	if !ok {
		t.Fatal("expected a valid shell-safe envelope")
	}
	if payload != "pytest -q" {
		t.Fatalf("payload mismatch: %q", payload)
	}
	if executionID != "exec-1234-5678" {
		t.Fatalf("execution_id mismatch: %q", executionID)
	}
}

func TestParseExecEnvelope_ShellWrappedToken(t *testing.T) {
	raw := "cd /workspace && __CBX_EXEC_1__exec-1234-5678\npytest -q"
	payload, executionID, ok := parseExecEnvelope(raw)
	if !ok {
		t.Fatal("expected a valid shell-wrapped envelope")
	}
	if payload != "cd /workspace && pytest -q" {
		t.Fatalf("payload mismatch: %q", payload)
	}
	if executionID != "exec-1234-5678" {
		t.Fatalf("execution_id mismatch: %q", executionID)
	}
}

func TestParseExecEnvelope_InvalidToken(t *testing.T) {
	raw := "__CBX_EXEC_1__exec id with spaces\necho hi"
	payload, _, ok := parseExecEnvelope(raw)
	if ok {
		t.Fatal("invalid token should degrade to raw command")
	}
	if payload != raw {
		t.Fatalf("expected raw command unchanged, got %q", payload)
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

// execTestChannel deliberately never reaches stdin EOF. Real SSH clients keep
// the channel open while waiting for exit-status, which exposed the Cmd.Wait
// cycle fixed by runCommand's independently managed stdin pipe.
type execTestChannel struct {
	stdout bytes.Buffer
	stderr bytes.Buffer
}

func (c *execTestChannel) Read([]byte) (int, error)                       { select {} }
func (c *execTestChannel) Write(p []byte) (int, error)                    { return c.stdout.Write(p) }
func (c *execTestChannel) Close() error                                   { return nil }
func (c *execTestChannel) CloseWrite() error                              { return nil }
func (c *execTestChannel) SendRequest(string, bool, []byte) (bool, error) { return true, nil }
func (c *execTestChannel) Stderr() io.ReadWriter                          { return &c.stderr }

func TestRunCommandDoesNotWaitForSSHStdinEOF(t *testing.T) {
	channel := &execTestChannel{}
	workdir := t.TempDir()
	done := make(chan executionLog, 1)
	go func() {
		done <- runCommand(channel, "printf command-finished", workdir, time.Second, 1024)
	}()
	select {
	case record := <-done:
		if record.ExitCode != 0 {
			t.Fatalf("command exit code = %d", record.ExitCode)
		}
		if channel.stdout.String() != "command-finished" {
			t.Fatalf("stdout = %q", channel.stdout.String())
		}
	case <-time.After(3 * time.Second):
		t.Fatal("runCommand deadlocked waiting for SSH stdin EOF")
	}
}

type stdinTestChannel struct {
	reader *bytes.Reader
	stdout bytes.Buffer
	stderr bytes.Buffer
}

func (c *stdinTestChannel) Read(p []byte) (int, error)                     { return c.reader.Read(p) }
func (c *stdinTestChannel) Write(p []byte) (int, error)                    { return c.stdout.Write(p) }
func (c *stdinTestChannel) Close() error                                   { return nil }
func (c *stdinTestChannel) CloseWrite() error                              { return nil }
func (c *stdinTestChannel) SendRequest(string, bool, []byte) (bool, error) { return true, nil }
func (c *stdinTestChannel) Stderr() io.ReadWriter                          { return &c.stderr }

func TestRunCommandStreamsSSHStdin(t *testing.T) {
	channel := &stdinTestChannel{reader: bytes.NewReader([]byte("archive-payload"))}
	record := runCommand(channel, "cat", t.TempDir(), time.Second, 1024)
	if record.ExitCode != 0 {
		t.Fatalf("command exit code = %d", record.ExitCode)
	}
	if channel.stdout.String() != "archive-payload" {
		t.Fatalf("stdout = %q", channel.stdout.String())
	}
}
