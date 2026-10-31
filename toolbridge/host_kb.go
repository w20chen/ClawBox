package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

func predictionReceipt(command string) string { return envelopeField(command, "prediction_receipt") }

var knowledgeClient = &http.Client{CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}

func envelopeField(command, field string) string {
	marker := strings.Index(command, clawboxExecEnvelopePrefix)
	if marker < 0 {
		return ""
	}
	header := strings.SplitN(command[marker+len(clawboxExecEnvelopePrefix):], "\n", 2)[0]
	if !strings.HasPrefix(header, "b64:") {
		return ""
	}
	data, err := base64.RawURLEncoding.DecodeString(strings.TrimPrefix(header, "b64:"))
	if err != nil {
		return ""
	}
	var envelope map[string]json.RawMessage
	if json.Unmarshal(data, &envelope) != nil {
		return ""
	}
	var value string
	_ = json.Unmarshal(envelope[field], &value)
	return value
}

func postKnowledge(observation map[string]any) (string, int64, error) {
	encoded, err := json.Marshal(observation)
	if err != nil {
		return "", 0, err
	}
	if len(encoded) > 1048576 {
		return "", 0, fmt.Errorf("compact observation exceeds 1048576 bytes")
	}
	seconds, err := strconv.ParseFloat(os.Getenv("CLAWTUNE_HOST_KB_TIMEOUT_SECONDS"), 64)
	if err != nil || seconds <= 0 {
		seconds = 30
	}
	attempts := envInt("CLAWBOX_KB_OBSERVATION_RETRIES", 2) + 1
	deadline, cancel := context.WithTimeout(context.Background(), time.Duration(seconds*float64(time.Second)))
	defer cancel()
	endpoint := strings.TrimRight(os.Getenv("CLAWTUNE_HOST_KB_ENDPOINT"), "/") + "/v1/kb/observe"
	var last error
	for i := 0; i < attempts && deadline.Err() == nil; i++ {
		attempt, stop := context.WithTimeout(deadline, time.Duration(seconds*float64(time.Second)/float64(attempts)))
		req, makeErr := http.NewRequestWithContext(attempt, "POST", endpoint, bytes.NewReader(encoded))
		if makeErr != nil {
			stop()
			return "", 0, makeErr
		}
		req.Header.Set("Authorization", "Bearer "+os.Getenv("CLAWTUNE_HOST_KB_TOKEN"))
		req.Header.Set("Content-Type", "application/json")
		response, sendErr := knowledgeClient.Do(req)
		if sendErr == nil {
			body, readErr := io.ReadAll(io.LimitReader(response.Body, 65537))
			response.Body.Close()
			stop()
			if readErr == nil && response.StatusCode == 200 && len(body) <= 65536 {
				var ack struct {
					Status     string `json:"status"`
					Generation int64  `json:"scope_generation"`
				}
				if json.Unmarshal(body, &ack) == nil && (ack.Status == "accepted" || ack.Status == "duplicate") && ack.Generation > 0 {
					return ack.Status, ack.Generation, nil
				}
				return "", 0, fmt.Errorf("invalid host KB acknowledgement")
			}
			if response.StatusCode >= 400 && response.StatusCode < 500 {
				return "", 0, fmt.Errorf("host KB rejected observation: HTTP %d", response.StatusCode)
			}
			last = fmt.Errorf("host KB update failed: HTTP %d", response.StatusCode)
		} else {
			stop()
			last = sendErr
		}
	}
	return "", 0, fmt.Errorf("host KB retries exhausted: %v", last)
}

func (c *guestCollectorClient) FinishWithResources(executionID string, returnCode int, receipt string, stats resourceStats, timedOut, cancelled bool, toolName string) (guestCollectorResponse, error) {
	// Struct omitempty omits valid measured zero: restore those fields explicitly.
	data, err := json.Marshal(stats)
	if err != nil {
		return guestCollectorResponse{}, err
	}
	var resources map[string]any
	if err := json.Unmarshal(data, &resources); err != nil {
		return guestCollectorResponse{}, err
	}
	if stats.MemoryEligible {
		resources["memory_baseline_bytes"] = stats.MemoryBaselineBytes
		resources["memory_total_peak_bytes"] = stats.MemoryTotalPeakBytes
		resources["memory_extra_peak_bytes"] = stats.MemoryExtraPeakBytes
	}
	return c.request(map[string]any{"op": "finish", "execution_id": executionID, "return_code": returnCode,
		"prediction_receipt": receipt, "resource_stats": resources, "timed_out": timedOut, "cancelled": cancelled, "tool_name": toolName,
		"repository": os.Getenv("CLAWBOX_REPOSITORY"), "session_id": os.Getenv("CLAWTUNE_HOST_KB_SESSION_ID"),
		"retain_artifact": os.Getenv("CLAWBOX_RETAIN_CLAUSE_TELEMETRY") == "1"})
}
