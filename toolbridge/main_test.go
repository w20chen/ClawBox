package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"
)

func TestParseExecEnvelope_PlainCommand(t *testing.T) {
	payload, executionID, profileCommand, ok := parseExecEnvelope("echo hello")
	if ok {
		t.Fatal("plain command should not report an envelope")
	}
	if payload != "echo hello" {
		t.Fatalf("payload mismatch: %q", payload)
	}
	if executionID != "" {
		t.Fatalf("expected empty execution_id, got %q", executionID)
	}
	if profileCommand != "echo hello" {
		t.Fatalf("profile command mismatch: %q", profileCommand)
	}
}

func TestParseExecEnvelope_Valid(t *testing.T) {
	raw := "__CBX_EXEC_1__{\"v\":1,\"execution_id\":\"exec-1234-5678\"}\npytest -q"
	payload, executionID, profileCommand, ok := parseExecEnvelope(raw)
	if !ok {
		t.Fatal("expected a valid envelope")
	}
	if payload != "pytest -q" {
		t.Fatalf("payload mismatch: %q", payload)
	}
	if executionID != "exec-1234-5678" {
		t.Fatalf("execution_id mismatch: %q", executionID)
	}
	if profileCommand != payload {
		t.Fatalf("legacy envelope should profile payload: %q", profileCommand)
	}
}

func TestParseExecEnvelope_ShellSafeToken(t *testing.T) {
	raw := "__CBX_EXEC_1__exec-1234-5678\npytest -q"
	payload, executionID, profileCommand, ok := parseExecEnvelope(raw)
	if !ok {
		t.Fatal("expected a valid shell-safe envelope")
	}
	if payload != "pytest -q" {
		t.Fatalf("payload mismatch: %q", payload)
	}
	if executionID != "exec-1234-5678" {
		t.Fatalf("execution_id mismatch: %q", executionID)
	}
	if profileCommand != payload {
		t.Fatalf("legacy envelope should profile payload: %q", profileCommand)
	}
}

func TestParseExecEnvelope_ShellWrappedToken(t *testing.T) {
	raw := "cd /workspace && __CBX_EXEC_1__exec-1234-5678\npytest -q"
	payload, executionID, profileCommand, ok := parseExecEnvelope(raw)
	if !ok {
		t.Fatal("expected a valid shell-wrapped envelope")
	}
	if payload != "cd /workspace && pytest -q" {
		t.Fatalf("payload mismatch: %q", payload)
	}
	if executionID != "exec-1234-5678" {
		t.Fatalf("execution_id mismatch: %q", executionID)
	}
	if profileCommand != payload {
		t.Fatalf("legacy envelope should profile payload: %q", profileCommand)
	}
}

func TestParseExecEnvelope_Base64ProfileCommand(t *testing.T) {
	profile := "printf ok"
	envelope, err := json.Marshal(execEnvelope{
		Version:           1,
		ExecutionID:       "exec-profile-1",
		ProfileCommandB64: base64.RawURLEncoding.EncodeToString([]byte(profile)),
	})
	if err != nil {
		t.Fatal(err)
	}
	raw := "env PATH=/session/bin CLAWTUNE_EXECUTION_ID=exec-profile-1 /bin/sh -c '" +
		clawboxExecEnvelopePrefix + "b64:" + base64.RawURLEncoding.EncodeToString(envelope) +
		"\nprintf ok'"
	payload, executionID, profileCommand, ok := parseExecEnvelope(raw)
	if !ok {
		t.Fatal("expected a valid base64 envelope")
	}
	if payload != "env PATH=/session/bin CLAWTUNE_EXECUTION_ID=exec-profile-1 /bin/sh -c 'printf ok'" {
		t.Fatalf("effective payload mismatch: %q", payload)
	}
	if executionID != "exec-profile-1" {
		t.Fatalf("execution_id mismatch: %q", executionID)
	}
	if profileCommand != profile {
		t.Fatalf("profile command mismatch: %q", profileCommand)
	}
}

func TestParseExecEnvelope_InvalidToken(t *testing.T) {
	raw := "__CBX_EXEC_1__exec id with spaces\necho hi"
	payload, _, _, ok := parseExecEnvelope(raw)
	if ok {
		t.Fatal("invalid token should degrade to raw command")
	}
	if payload != raw {
		t.Fatalf("expected raw command unchanged, got %q", payload)
	}
}

func TestParseExecEnvelope_PayloadMayContainNewlines(t *testing.T) {
	raw := "__CBX_EXEC_1__{\"v\":1,\"execution_id\":\"exec-abc\"}\nprintf 'a\nb\n'"
	payload, executionID, profileCommand, ok := parseExecEnvelope(raw)
	if !ok {
		t.Fatal("expected a valid envelope")
	}
	if payload != "printf 'a\nb\n'" {
		t.Fatalf("payload mismatch: %q", payload)
	}
	if executionID != "exec-abc" {
		t.Fatalf("execution_id mismatch: %q", executionID)
	}
	if profileCommand != payload {
		t.Fatalf("legacy envelope should profile payload: %q", profileCommand)
	}
}

func TestParseExecEnvelope_MalformedJSON(t *testing.T) {
	raw := "__CBX_EXEC_1__{not-json}\necho hi"
	payload, _, _, ok := parseExecEnvelope(raw)
	if ok {
		t.Fatal("malformed JSON should degrade to raw command (ok=false)")
	}
	if payload != raw {
		t.Fatalf("expected raw command unchanged, got %q", payload)
	}
}

func TestParseExecEnvelope_WrongVersion(t *testing.T) {
	raw := "__CBX_EXEC_1__{\"v\":2,\"execution_id\":\"exec-x\"}\necho hi"
	payload, _, _, ok := parseExecEnvelope(raw)
	if ok {
		t.Fatal("wrong envelope version should degrade to raw command")
	}
	if payload != raw {
		t.Fatalf("expected raw command unchanged, got %q", payload)
	}
}

func TestParseExecEnvelope_EmptyExecutionID(t *testing.T) {
	raw := "__CBX_EXEC_1__{\"v\":1,\"execution_id\":\"\"}\necho hi"
	payload, _, _, ok := parseExecEnvelope(raw)
	if ok {
		t.Fatal("empty execution_id should degrade to raw command")
	}
	if payload != raw {
		t.Fatalf("expected raw command unchanged, got %q", payload)
	}
}

func TestParseExecEnvelope_NoNewline(t *testing.T) {
	raw := "__CBX_EXEC_1__{\"v\":1,\"execution_id\":\"exec-x\"}"
	payload, _, _, ok := parseExecEnvelope(raw)
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

func TestRunCommandProfilesLogicalCommandButExecutesWrapper(t *testing.T) {
	channel := &execTestChannel{}
	profile := "printf logical"
	envelope, err := json.Marshal(execEnvelope{
		Version:           1,
		ExecutionID:       "exec-profile-2",
		ProfileCommandB64: base64.RawURLEncoding.EncodeToString([]byte(profile)),
	})
	if err != nil {
		t.Fatal(err)
	}
	effective := "printf wrapper-executed"
	raw := "printf wrapper-" + clawboxExecEnvelopePrefix + "b64:" +
		base64.RawURLEncoding.EncodeToString(envelope) + "\nexecuted"
	record := runCommand(channel, raw, t.TempDir(), time.Second, 1024)
	if record.ExitCode != 0 || channel.stdout.String() != "wrapper-executed" {
		t.Fatalf("effective command was not executed: exit=%d stdout=%q", record.ExitCode, channel.stdout.String())
	}
	logicalDigest := sha256.Sum256([]byte(profile))
	effectiveDigest := sha256.Sum256([]byte(effective))
	if record.CommandSHA256 != hex.EncodeToString(logicalDigest[:]) {
		t.Fatalf("logical digest mismatch: %s", record.CommandSHA256)
	}
	if record.EffectiveSHA256 != hex.EncodeToString(effectiveDigest[:]) {
		t.Fatalf("effective digest mismatch: %s", record.EffectiveSHA256)
	}
	if record.CommandBytes != len(profile) || record.EffectiveBytes != len(effective) {
		t.Fatalf("command byte counts mismatch: logical=%d effective=%d", record.CommandBytes, record.EffectiveBytes)
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

func TestCancellationBypassesOccupiedCommandSlot(t *testing.T) {
	cancelled := make(chan struct{}, 1)
	activeExecutions.Store("exec-active", cancelled)
	defer activeExecutions.Delete("exec-active")
	semaphore := make(chan struct{}, 1)
	semaphore <- struct{}{}
	requests := make(chan *ssh.Request, 1)
	requests <- &ssh.Request{Type: "exec", Payload: ssh.Marshal(struct{ Command string }{cancelCommandPrefix + "exec-active"})}
	close(requests)
	done := make(chan struct{})
	go func() {
		handleSession(&execTestChannel{}, requests, t.TempDir(), time.Minute, 1024, semaphore)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("cancellation waited for the command it must stop")
	}
	if len(cancelled) != 1 || len(semaphore) != 1 {
		t.Fatal("cancellation did not preserve command slot ownership")
	}
}

func TestRunCommandCancellationReapsBeforeReturning(t *testing.T) {
	channel := &stdinTestChannel{reader: bytes.NewReader(nil)}
	done := make(chan executionLog, 1)
	go func() {
		done <- runCommand(channel, "__CBX_EXEC_1__exec-cancel-test\nsleep 30", t.TempDir(), time.Minute, 1024)
	}()
	deadline := time.Now().Add(5 * time.Second)
	for !cancelExecution("exec-cancel-test") {
		if time.Now().After(deadline) {
			t.Fatal("execution was not registered")
		}
		time.Sleep(time.Millisecond)
	}
	select {
	case record := <-done:
		if !record.Cancelled || record.TimedOut || record.ExitCode != 130 {
			t.Fatalf("wrong cancellation result: %+v", record)
		}
		if cancelExecution("exec-cancel-test") {
			t.Fatal("completed execution remains active")
		}
	case <-time.After(10 * time.Second):
		t.Fatal("cancelled process was not reaped")
	}
}

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
