package main

import (
	"bufio"
	"encoding/json"
	"net"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

func TestGuestCollectorClientAuthenticatedLifecycle(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix sockets are a guest-only contract")
	}
	socket := filepath.Join(t.TempDir(), "collector.sock")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	token := "0123456789abcdef0123456789abcdef"
	requests := make(chan map[string]any, 3)
	go func() {
		for index := 0; index < 3; index++ {
			connection, acceptErr := listener.Accept()
			if acceptErr != nil {
				return
			}
			var request map[string]any
			_ = json.NewDecoder(bufio.NewReader(connection)).Decode(&request)
			requests <- request
			response := map[string]any{"ok": true, "v": 1}
			if request["op"] == "begin" {
				response["artifact_path"] = "/artifacts/clause-telemetry-exec-1.json"
			}
			if request["op"] == "finish" {
				response["eligible_for_kb"] = true
				response["collection_validity"] = "valid"
				response["cleanup"] = "ok"
			}
			_ = json.NewEncoder(connection).Encode(response)
			_ = connection.Close()
		}
	}()

	client := &guestCollectorClient{socket: socket, token: token, timeout: time.Second}
	begin, err := client.Begin("exec-1", "echo ok", "/sys/fs/cgroup/clawbox-exec-1", 42, "owner/repo")
	if err != nil || begin.ArtifactPath == "" {
		t.Fatalf("begin failed: response=%+v error=%v", begin, err)
	}
	finish, err := client.Finish("exec-1", 0)
	if err != nil || !finish.EligibleForKB || finish.CollectionValidity != "valid" {
		t.Fatalf("finish failed: response=%+v error=%v", finish, err)
	}
	if err := client.Abort("exec-1"); err != nil {
		t.Fatalf("abort failed: %v", err)
	}

	for _, operation := range []string{"begin", "finish", "abort"} {
		request := <-requests
		if request["op"] != operation || request["token"] != token || request["v"] != float64(1) {
			t.Fatalf("unexpected %s request: %#v", operation, request)
		}
	}
}

func TestGuestCollectorClientRejectsProtocolErrors(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix sockets are a guest-only contract")
	}
	for _, response := range []map[string]any{
		{"ok": false, "v": 1, "error": "authentication_failed"},
		{"ok": true, "v": 2},
	} {
		socket := filepath.Join(t.TempDir(), "collector.sock")
		listener, err := net.Listen("unix", socket)
		if err != nil {
			t.Fatal(err)
		}
		go func() {
			connection, _ := listener.Accept()
			if connection != nil {
				_, _ = bufio.NewReader(connection).ReadBytes('\n')
				_ = json.NewEncoder(connection).Encode(response)
				_ = connection.Close()
			}
			_ = listener.Close()
		}()
		client := &guestCollectorClient{socket: socket, token: "token", timeout: time.Second}
		if _, err := client.request(map[string]any{"op": "health"}); err == nil {
			t.Fatalf("expected response rejection for %#v", response)
		}
	}
}

func TestRandomCollectorTokenHasRequiredEntropy(t *testing.T) {
	first, err := randomCollectorToken()
	if err != nil {
		t.Fatal(err)
	}
	second, err := randomCollectorToken()
	if err != nil {
		t.Fatal(err)
	}
	if len(first) != 64 || len(second) != 64 || first == second {
		t.Fatalf("unexpected tokens: %q %q", first, second)
	}
}
