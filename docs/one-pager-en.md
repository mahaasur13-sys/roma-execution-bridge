# DecisionOS + ComputeOS
## AstroFin Sentinel V5 + ROMA Execution Bridge v1.2.0

---

### What It Is

A **multi-agent decision platform** where 13+ AI agents function as a "board of directors" — each analyzes the market from a different angle and votes with a weighted score. A formal arbiter (KARL) resolves conflicts and produces a final recommendation with a complete audit trail.

Paired with **ROMA** — a closed-loop GPU orchestration platform that handles compute, cost, execution, and billing.

---

### How It Works

```
User Query → Router → 13 Parallel Agents → KARL Synthesis → BUY/SELL Signal
                 ↑                                         ↓
            RAG Knowledge Layer                    Audit Trail (JSONL)
```

| Layer | Description |
|-------|-------------|
| **RAG-First** | Every signal goes through a knowledge layer (102 documents, 1981 chunks, FAISS+BM25) before reaching agents |
| **Agent Council** | 13 agents vote with fixed weights: Fundamental (20%), Quant (20%), Macro (15%), Options (15%), Sentiment (10%), Technical (10%), Astro (16%) |
| **KARL Arbitration** | Conflict resolution: when Astro contradicts Fundamental+Quant → Astro weight −30%, Fundamental +18%, Quant +12% |
| **Audit Trail** | Every decision is recorded — timestamp, agent votes, confidence, adjustments, reasoning |
| **AMRE Learning** | Thompson Sampling selects agents dynamically; Bayesian belief tracking updates with every session |

---

### Why It's Different

| Feature | AstroFin | Typical Trading Bot |
|---------|----------|---------------------|
| Signal sources | 13 agents × 5 domains | 1–2 indicators |
| Conflict handling | Formal arbitration | Silent averaging |
| Audit trail | Full DecisionRecord | None |
| Self-optimization | Thompson Sampling + Bayesian | Static weights |
| Knowledge grounding | RAG (1981 chunks) | None |
| GPU compute | Built-in (ROMA) | External |
| Domain flexibility | Trading, Medical, Legal, Supply Chain | Trading only |

---

### Pricing

| Tier | Price | Includes |
|------|-------|----------|
| **Start** | **$49/mo** | AstroFin signals + 50 ROMA jobs + REST API + Telegram alerts + Audit trail |
| **Pro** | **$89/mo** | Everything in Start + 150 jobs + GPU access + Priority queue + Custom agent weights |
| **Backtest** | **$29** (one-time) | Walk-forward backtest with full KPI report (15–30 min) |
| **White-Label** | **$799–$1,499** (30 days) | Custom tenant + agent pool + branded dashboard + domain adaptation + support |

---

### What You Get Immediately

- ROMA API key (port 8900)
- Access to AstroFin signals
- Decision Gate: Quota + Cost + Policy enforcement
- Full audit trail — every decision, every agent vote

---

### Live Infrastructure (August 2026)

| Service | Status |
|---------|:------:|
| AstroFin API (8000) | ✅ UP |
| ROMA API (8900) | ✅ UP |
| PostgreSQL (5432) | ✅ UP |
| Grafana (3000) | ✅ UP |
| Prometheus (9090) | ✅ UP |
| Evolution Engine (9192) | ✅ UP |

**Availability:** 99.5%+ uptime. Stress-tested at 500 concurrent users (spike test passed without crashes).

---

### Contact

**Telegram:** [@asurdev](https://t.me/asurdev)
**Landing:** [asurdev.zo.space/offer-en](https://asurdev.zo.space/offer-en)
**API:** `astrofin-api-asurdev.zocomputer.io`
**ROMA:** `roma-execution-bridge-asurdev.zocomputer.io`

*AstroFin Sentinel V5 + ROMA Execution Bridge v1.2.0 · August 2026*
