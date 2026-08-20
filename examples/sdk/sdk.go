// ROMA Execution Bridge v2.1.0 — Go SDK
// Full billing flow: GPU-sec + tokens + spend-caps + 402 handling.
//
// Usage: go run sdk.go
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"
)

const BaseURL = "http://localhost:8900"
const APIKey = "your-api-key-here"

// ── Types ──────────────────────────────────────────────────────────────

type JobSubmit struct {
	Task         string `json:"task"`
	GPURequired  bool   `json:"gpu_required"`
	GPUType      string `json:"gpu_type,omitempty"`
	InputTokens  int    `json:"input_tokens"`
	OutputTokens int    `json:"output_tokens"`
	Plan         string `json:"plan"`
	Priority     int    `json:"priority"`
}

type JobResponse struct {
	JobID              string  `json:"job_id"`
	Status             string  `json:"status"`
	EstimatedCostUSD   float64 `json:"estimated_cost_usd,omitempty"`
	SpendCapRemaining  float64 `json:"spend_cap_remaining,omitempty"`
}

type JobStatus struct {
	JobID  string `json:"job_id"`
	Status string `json:"status"`
}

type BillingUsage struct {
	BalanceUSD      float64 `json:"balance_usd"`
	Plan            string  `json:"plan"`
	SpendCapUSD     float64 `json:"spend_cap_usd"`
	TotalGPUSeconds float64 `json:"total_gpu_seconds"`
	TotalInputTokens  int   `json:"total_input_tokens"`
	TotalOutputTokens int   `json:"total_output_tokens"`
	TotalCostUSD    float64 `json:"total_cost_usd"`
	Alert90Pct      bool    `json:"alert_90pct"`
}

type SpendCapError struct {
	Message      string  `json:"detail"`
	RemainingCap float64 `json:"remaining_cap"`
}

func (e *SpendCapError) Error() string {
	return fmt.Sprintf("spend cap exceeded: %s (remaining: $%.4f)", e.Message, e.RemainingCap)
}

// ── Client ────────────────────────────────────────────────────────────

type ROMAClient struct {
	baseURL string
	apiKey  string
	client  *http.Client
}

func NewROMAClient(baseURL, apiKey string) *ROMAClient {
	return &ROMAClient{
		baseURL: baseURL,
		apiKey:  apiKey,
		client:  &http.Client{Timeout: 30 * time.Second},
	}
}

func (c *ROMAClient) request(method, path string, body interface{}) ([]byte, error) {
	var reqBody io.Reader
	if body != nil {
		data, _ := json.Marshal(body)
		reqBody = bytes.NewReader(data)
	}

	req, _ := http.NewRequest(method, c.baseURL+path, reqBody)
	req.Header.Set("x-api-key", c.apiKey)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	data, _ := io.ReadAll(resp.Body)

	if resp.StatusCode == 402 {
		var capErr SpendCapError
		json.Unmarshal(data, &capErr)
		return nil, &capErr
	}
	if resp.StatusCode == 401 {
		return nil, fmt.Errorf("invalid API key")
	}
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(data))
	}
	return data, nil
}

// ── API Methods ──────────────────────────────────────────────────────

func (c *ROMAClient) SubmitJob(task string, gpuRequired bool, gpuType string,
	inputTokens, outputTokens int, plan string) (*JobResponse, error) {

	payload := JobSubmit{
		Task: task, GPURequired: gpuRequired, GPUType: gpuType,
		InputTokens: inputTokens, OutputTokens: outputTokens,
		Plan: plan, Priority: 5,
	}
	data, err := c.request("POST", "/submit", payload)
	if err != nil {
		return nil, err
	}
	var job JobResponse
	json.Unmarshal(data, &job)
	return &job, nil
}

func (c *ROMAClient) JobStatus(jobID string) (*JobStatus, error) {
	data, err := c.request("GET", "/status/"+jobID, nil)
	if err != nil {
		return nil, err
	}
	var status JobStatus
	json.Unmarshal(data, &status)
	return &status, nil
}

func (c *ROMAClient) CompleteJob(jobID string) error {
	_, err := c.request("POST", "/complete/"+jobID, nil)
	return err
}

func (c *ROMAClient) GetUsage() (*BillingUsage, error) {
	data, err := c.request("GET", "/usage", nil)
	if err != nil {
		return nil, err
	}
	var usage BillingUsage
	json.Unmarshal(data, &usage)
	return &usage, nil
}

// ═══════════════════════════════════════════════════════════════════════
//  Example: Full job lifecycle with billing
// ═══════════════════════════════════════════════════════════════════════

func main() {
	client := NewROMAClient(BaseURL, "roma-demo-key-2026")

	// 1. Check balance before submitting
	usage, err := client.GetUsage()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Usage check failed: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("Balance: $%.4f  Plan: %s  Spend cap: $%.2f\n",
		usage.BalanceUSD, usage.Plan, usage.SpendCapUSD)

	// 2. Submit LLM job with tokens
	job, err := client.SubmitJob(
		"Summarize quarterly report (LLM inference)",
		true, "A100", 8000, 2000, "pro",
	)
	if err != nil {
		if capErr, ok := err.(*SpendCapError); ok {
			fmt.Fprintf(os.Stderr, "❌ SPEND CAP EXCEEDED: %v\n", capErr)
			fmt.Fprintln(os.Stderr, "   Upgrade plan or wait for billing cycle reset.")
			os.Exit(1)
		}
		fmt.Fprintf(os.Stderr, "Submit failed: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("Job created: %s  Cost: $%.6f  Remaining: $%.6f\n",
		job.JobID, job.EstimatedCostUSD, job.SpendCapRemaining)

	// 3. Poll status
	for i := 0; i < 5; i++ {
		status, _ := client.JobStatus(job.JobID)
		fmt.Printf("  Status: %s\n", status.Status)
		if status.Status == "completed" {
			break
		}
		time.Sleep(2 * time.Second)
	}

	// 4. Complete job — actual billing
	client.CompleteJob(job.JobID)

	// 5. Final balance
	usage2, _ := client.GetUsage()
	fmt.Printf("Final balance: $%.4f\n", usage2.BalanceUSD)
	fmt.Printf("Charged: $%.6f\n", usage2.BalanceUSD-usage.BalanceUSD)
}
