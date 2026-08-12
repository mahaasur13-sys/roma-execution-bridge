# ROMA Beta Testing Program

## Goals

- Validate core execution flow (submit → status → cancel) with real workloads
- Identify missing features and pain points
- Build initial user base (target: 15–20 beta testers)
- Gather testimonials and performance metrics

## Target Audience

| Role | % | Use Cases |
|------|:--:|-----------|
| ML Engineers | 35% | Model training, batch inference |
| Researchers | 25% | Experimentation, hyperparameter search |
| MLOps/DevOps | 20% | Pipeline automation, scheduling |
| CTO/Leads | 15% | Team productivity, cost optimization |
| Students | 5% | Academic research, dissertations |

## Selection Criteria

1. Has a real GPU workload (not just curious)
2. Willing to provide feedback (survey + 1 call)
3. Active within first week
4. Technical proficiency (can use API/CLI)

## Application Process

1. **Collect** — via `/beta` page (public form)
2. **Review** — check use_case field, prioritize real projects
3. **Invite** — send email with dashboard link + demo key
4. **Onboard** — schedule 15-minute call for first 5 testers
5. **Monitor** — track `jobs` table: who submits, how often
6. **Follow-up** — survey after 2 weeks

## Feedback Collection

| Method | Timing | Format |
|--------|--------|--------|
| Onboarding call | Day 0 | 15 min, open-ended |
| Mid-beta survey | Week 2 | Google Form / Typeform |
| Exit interview | Week 4 | 20 min, structured |

## Success Metrics

| Metric | Target |
|--------|:------:|
| Applications received | ≥ 20 |
| Accepted testers | ≥ 12 |
| Active testers (≥1 task in week 1) | ≥ 8 |
| Feedback responses | ≥ 6 |
| NPS score | ≥ 40 |
| Bugs found + fixed | ≥ 3 |

## Timeline

| Week | Activity |
|------|----------|
| 1 | Collect applications → invite first batch |
| 2 | Onboarding calls + support |
| 3–4 | Mid-beta survey → iterate |
| 4+ | Evaluate → decide on public launch |


## First Wave — Beta Invitations (2026-08-12)

**Mode:** Dry-run (SENDGRID_API_KEY not configured)

| Metric | Value |
|--------|:-----:|
| Leads processed | 10 |
| Emails sent (dry-run) | 10 |
| Failed | 0 |
| Delivery rate | 100% |
| Leads status updated to 'invited' | 10/10 |

### Test Webhook Events (simulated)

| Event | Email | Status |
|-------|-------|:------:|
| delivered | alex@ml-startup.io | ✅ processed |
| open | alex@ml-startup.io | ✅ processed |
| click | alex@ml-startup.io | ✅ processed |

### Email Statistics (post-send)

| Metric | Value |
|--------|:-----:|
| Total sent | 10 |
| Opened | 1 |
| Clicked | 1 |
| Open rate | 10.0% |
| Click rate | 10.0% |

### Lead Details

| # | Email | Company | Role | Use Case |
|---|-------|---------|------|----------|
| 1 | alex@ml-startup.io | ML Startup | CTO | GPU training pipeline |
| 2 | maria@dataflow.com | DataFlow Inc | ML Engineer | Batch inference jobs |
| 3 | dmitry@cloudlab.dev | CloudLab | DevOps Lead | K8s GPU orchestration |
| 4 | elena@airesearch.org | AI Research Lab | Researcher | LLM fine-tuning |
| 5 | sergey@quantcore.ru | QuantCore | Quant Developer | Monte Carlo simulations |
| 6 | anna@biotechml.com | BioTech ML | Data Scientist | Protein folding |
| 7 | pavel@startupx.io | StartupX | Founder | Cost-aware ML infra |
| 8 | olga@fintech.ai | FinTech AI | VP Engineering | Fraud detection pipelines |
| 9 | ivan@robotics.dev | Robotics Lab | Research Engineer | Reinforcement learning |
| 10 | nina@edtech.ai | EdTech AI | Head of AI | Student model training |

### Invitation Template

- HTML: Professional dark theme (GitHub-style), responsive
- Plain text: Included as alternative
- Personalization: `{ name }` (company name fallback), `{ email }`
- CTA: Direct link to dashboard with UTM tracking
- Demo key: `roma-demo-key-2026`
- Template: `docs/beta-invitation-template.md`
- Script: `scripts/send_invitations.py`

### Next Steps

1. **Enable real sends:** Configure `SENDGRID_API_KEY` and `FROM_EMAIL` in `.env`
2. **Re-seed leads:** Reset `leads.status` to `'new'` for the 10 test leads
3. **Send real wave:** `python scripts/send_invitations.py --limit 10`
4. **Monitor:** Track email open/click via SendGrid webhook + `/admin/email-stats`
5. **Onboard:** Schedule calls with engaged testers
