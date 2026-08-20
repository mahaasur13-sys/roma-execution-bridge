/**
 * ROMA Execution Bridge v2.1.0 — JavaScript/Node.js SDK
 * Full billing flow: GPU-sec + tokens + spend-caps + 402 handling.
 */

const BASE_URL = "http://localhost:8900";
const API_KEY = "your-api-key-here";

class SpendCapExceeded extends Error {
  constructor(message, remaining) {
    super(message);
    this.name = "SpendCapExceeded";
    this.remaining = remaining;
  }
}

class ROMAClient {
  constructor(baseUrl = BASE_URL, apiKey = API_KEY) {
    this.base = baseUrl.replace(/\/$/, "");
    this.apiKey = apiKey;
  }

  async _request(method, path, body = null) {
    const url = `${this.base}${path}`;
    const opts = {
      method,
      headers: { "x-api-key": this.apiKey, "Content-Type": "application/json" },
    };
    if (body) opts.body = JSON.stringify(body);
    const resp = await fetch(url, opts);

    if (resp.status === 402) {
      const data = await resp.json();
      throw new SpendCapExceeded(
        data.detail || "spend cap exceeded",
        data.remaining_cap
      );
    }
    if (resp.status === 401) throw new Error("Invalid API key");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  // ── Jobs ──────────────────────────────────────────────────────────
  async submitJob({ task, gpuRequired = false, gpuType = "any",
                     inputTokens = 0, outputTokens = 0, plan = "free",
                     priority = 5 } = {}) {
    return this._request("POST", "/submit", {
      task, gpu_required: gpuRequired, gpu_type: gpuType,
      input_tokens: inputTokens, output_tokens: outputTokens,
      plan, priority,
    });
  }

  async jobStatus(jobId) { return this._request("GET", `/status/${jobId}`); }
  async completeJob(jobId) { return this._request("POST", `/complete/${jobId}`); }
  async listJobs() { return this._request("GET", "/jobs"); }

  // ── Billing ──────────────────────────────────────────────────────
  async getUsage() { return this._request("GET", "/usage"); }

  async getBalance() {
    const usage = await this.getUsage();
    return usage.balance_usd || 0;
  }
}

// ═══════════════════════════════════════════════════════════════════════
//  Example: Full job lifecycle with billing
// ═══════════════════════════════════════════════════════════════════════

async function demoFullLifecycle() {
  const client = new ROMAClient(BASE_URL, "roma-demo-key-2026");

  // 1. Check balance before submitting
  let usage = await client.getUsage();
  console.log(`Balance: $${usage.balance_usd.toFixed(4)}  ` +
    `Plan: ${usage.plan}  Spend cap: $${usage.spend_cap_usd.toFixed(2)}`);

  // 2. Submit LLM job with tokens
  let job;
  try {
    job = await client.submitJob({
      task: "Summarize quarterly report (LLM inference)",
      gpuRequired: true,
      gpuType: "A100",
      inputTokens: 8000,
      outputTokens: 2000,
      plan: "pro",
    });
    console.log(`Job created: ${job.job_id}  ` +
      `Estimated cost: $${job.estimated_cost_usd}  ` +
      `Remaining cap: $${job.spend_cap_remaining}`);
  } catch (e) {
    if (e instanceof SpendCapExceeded) {
      console.error(`❌ SPEND CAP EXCEEDED: ${e.message}`);
      console.error("   Upgrade plan or wait for billing cycle reset.");
      process.exit(1);
    }
    throw e;
  }

  // 3. Poll until complete
  for (let i = 0; i < 5; i++) {
    const status = await client.jobStatus(job.job_id);
    console.log(`  Status: ${status.status}`);
    if (status.status === "completed") break;
    await new Promise(r => setTimeout(r, 2000));
  }

  // 4. Complete job — actual billing
  await client.completeJob(job.job_id);

  // 5. Final balance
  const usage2 = await client.getUsage();
  console.log(`Final balance: $${usage2.balance_usd.toFixed(4)}`);
  console.log(`Charged: $${(usage2.balance_usd - usage.balance_usd).toFixed(6)}`);
}

demoFullLifecycle().catch(console.error);
