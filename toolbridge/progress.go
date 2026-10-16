package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// Progress metadata is sent by the authenticated Runtime SSH wrapper. It does
// not alter the command or its profiling digest and is never persisted in logs.
type progressControl struct {
	URL     string         `json:"url"`
	Token   string         `json:"token"`
	Request map[string]any `json:"request"`
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
	body := make(map[string]any, len(p.Request)+1)
	for key, value := range p.Request {
		body[key] = value
	}
	body["phase"] = phase
	encoded, err := json.Marshal(body)
	if err != nil {
		return err
	}
	request, err := http.NewRequest("POST", strings.TrimRight(p.URL, "/")+"/v1/tool/start", bytes.NewReader(encoded))
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+p.Token)
	request.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 5 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	response, err := client.Do(request)
	if err != nil {
		return fmt.Errorf("progress %s delivery failed", phase)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("progress %s status %d", phase, response.StatusCode)
	}
	return nil
}
