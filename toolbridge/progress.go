package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/http/httptrace"
	"strings"
	"sync"
	"time"
)

// Progress metadata is sent by the authenticated Runtime SSH wrapper. It does
// not alter the command or its profiling digest and is never persisted in logs.
type progressControl struct {
	URL            string           `json:"url"`
	Token          string           `json:"token"`
	Request        map[string]any   `json:"request"`
	TimeoutSeconds float64          `json:"timeout_seconds"`
	Timings        []progressTiming `json:"-"`
}

// Offsets are relative to this attempt, measured with the guest monotonic
// clock. A missing offset means that stage was never reached. Never record
// endpoint addresses, credentials, response bodies or transport error text.
type progressTiming struct {
	Phase                 string   `json:"phase"`
	Attempt               int      `json:"attempt"`
	StartedUnixNS         int64    `json:"started_unix_ns"`
	Seconds               float64  `json:"seconds"`
	ConnectionSeconds     *float64 `json:"connection_seconds,omitempty"`
	RequestWrittenSeconds *float64 `json:"request_written_seconds,omitempty"`
	FirstByteSeconds      *float64 `json:"first_byte_seconds,omitempty"`
	Reused                bool     `json:"reused"`
	Status                int      `json:"status,omitempty"`
	TransportError        bool     `json:"transport_error,omitempty"`
}

func (p *progressControl) tracedRequest(request *http.Request, phase string, attempt int) (*http.Response, []byte, error) {
	started := time.Now()
	row := progressTiming{Phase: phase, Attempt: attempt, StartedUnixNS: started.UnixNano()}
	var mu sync.Mutex
	offset := func(target **float64) {
		mu.Lock()
		defer mu.Unlock()
		seconds := time.Since(started).Seconds()
		*target = &seconds
	}
	trace := &httptrace.ClientTrace{
		GotConn: func(info httptrace.GotConnInfo) {
			mu.Lock()
			defer mu.Unlock()
			seconds := time.Since(started).Seconds()
			row.ConnectionSeconds, row.Reused = &seconds, info.Reused
		},
		WroteRequest: func(info httptrace.WroteRequestInfo) {
			if info.Err == nil {
				offset(&row.RequestWrittenSeconds)
			}
		},
		GotFirstResponseByte: func() { offset(&row.FirstByteSeconds) },
	}
	response, err := progressClient.Do(request.WithContext(httptrace.WithClientTrace(request.Context(), trace)))
	var payload []byte
	if err == nil {
		payload, err = io.ReadAll(io.LimitReader(response.Body, 65537))
		response.Body.Close()
	}
	mu.Lock()
	row.Seconds, row.TransportError = time.Since(started).Seconds(), err != nil
	if response != nil {
		row.Status = response.StatusCode
	}
	p.Timings = append(p.Timings, row)
	mu.Unlock()
	return response, payload, err
}

// Reuse the transport across boundaries and executions. Reading the small
// acknowledgement to EOF lets HTTP/1.1 reuse its authenticated connection.
var progressClient = &http.Client{
	CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse },
}

func parseProgressControl(command string) *progressControl {
	marker := strings.Index(command, clawboxExecEnvelopePrefix)
	if marker < 0 {
		return nil
	}
	header := strings.SplitN(command[marker+len(clawboxExecEnvelopePrefix):], "\n", 2)[0]
	if !strings.HasPrefix(header, "b64:") {
		return nil
	}
	decoded, err := base64.RawURLEncoding.DecodeString(strings.TrimPrefix(header, "b64:"))
	if err != nil {
		return nil
	}
	var envelope struct {
		Progress *progressControl `json:"progress_control"`
	}
	if json.Unmarshal(decoded, &envelope) != nil {
		return nil
	}
	return envelope.Progress
}

func (p *progressControl) report(phase string) error {
	if p == nil {
		return nil
	}
	if p.TimeoutSeconds <= 0 || math.IsNaN(p.TimeoutSeconds) || math.IsInf(p.TimeoutSeconds, 0) {
		return fmt.Errorf("invalid progress acknowledgement deadline")
	}
	body := make(map[string]any, len(p.Request)+1)
	for key, value := range p.Request {
		body[key] = value
	}
	body["phase"] = phase
	encoded, err := json.Marshal(body)
	if err != nil {
		return err
	}
	started := time.Now()
	budget := time.Duration(p.TimeoutSeconds * float64(time.Second))
	deadline, stop := context.WithTimeout(context.Background(), budget)
	defer stop()
	var deliveryError error
	attempts := 0
	for attempts < 3 && deadline.Err() == nil {
		attempts++
		// Retrying an acknowledgement is safe: the host deduplicates the same
		// execution/phase. The payload command is never retried here.
		attempt, cancel := context.WithTimeout(deadline, budget/3)
		request, err := http.NewRequestWithContext(attempt, "POST", strings.TrimRight(p.URL, "/")+"/v1/tool/start", bytes.NewReader(encoded))
		if err != nil {
			cancel()
			return fmt.Errorf("invalid progress %s endpoint", phase)
		}
		request.Header.Set("Authorization", "Bearer "+p.Token)
		request.Header.Set("Content-Type", "application/json")
		response, payload, err := p.tracedRequest(request, phase, attempts)
		if response != nil {
			cancel()
			if response.StatusCode != http.StatusOK {
				// A policy rejection is authoritative, not a transport retry.
				return fmt.Errorf("progress %s status %d", phase, response.StatusCode)
			}
			if err == nil {
				var ack struct {
					Status      string `json:"status"`
					SessionID   string `json:"session_id"`
					ExecutionID string `json:"execution_id"`
				}
				if len(payload) > 65536 || json.Unmarshal(payload, &ack) != nil || ack.Status != "STARTED" || ack.SessionID != p.Request["session_id"] || ack.ExecutionID != p.Request["execution_id"] {
					return fmt.Errorf("progress %s acknowledgement does not match execution", phase)
				}
				return nil
			}
		} else {
			cancel()
		}
		deliveryError = err
	}
	cause := "transport"
	var networkError net.Error
	if errors.Is(deliveryError, context.DeadlineExceeded) || errors.As(deliveryError, &networkError) && networkError.Timeout() {
		cause = "timeout"
	}
	// Do not include the URL or capability token in guest error evidence.
	return fmt.Errorf("progress %s delivery failed: %s after %d attempts (%.3fs)", phase, cause, attempts, time.Since(started).Seconds())
}
