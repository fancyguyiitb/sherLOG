# Autonomous Root-Cause Analysis System — Architecture & Scope

**Constraint set:** 8 weeks total, 2 days to lock scope, free/low-cost infra, OpenAI as LLM, deployed with a demo frontend.

---

## 0. The strategic call (read this first)

IncidentFox is a **breadth** product: 45+ integrations, multi-tenant config service, gVisor K8s sandboxes, Envoy credential proxy, RAPTOR hierarchical RAG, a Slack/Teams/Chat surface, and a premium continuous-learning pipeline. It is several engineer-years of work. Cloning its shape in 8 weeks produces a shallow imitation of all of it and a working version of none of it.

But your constraint list contains an asset IncidentFox structurally *cannot* have:

> **You own the environment and you inject the faults. Therefore you know the true root cause of every incident.**

IncidentFox can never say "our RCA is 84% accurate," because in a customer's production environment nobody knows ground truth. You can. That single fact converts your vaguest requirement — *"guardrails against hallucination"* — from hand-waving into a **measured, graphed, defensible system property**.

**So the thesis of this project is not "an AI SRE." It is: *a verifiable* AI SRE — an agentic system whose correctness is continuously measured against injected ground truth, and which refuses to answer when evidence is insufficient.**

Everything below follows from that.

### What to take from the reference repo

| Take | Why |
|---|---|
| Skills-as-scripts pattern (`.claude/skills/*/SKILL.md` + `scripts/`) | Excellent context-economy idea. Tool descriptions load lazily; only script *output* enters context, not the integration code. Port this to OpenAI function-calling. |
| Statistics-before-sampling for logs | The one genuinely hard technical idea in the repo. Aggregate first, then targeted retrieval. Non-negotiable — you'll blow context otherwise. |
| Credential proxy concept (agent never holds secrets) | Directly answers your security requirement. Simplify from Envoy+JWT to an in-process egress broker. |
| Subagent context isolation | Keeps the main reasoning loop clean. Implement as sub-loops with their own context windows returning structured summaries. |
| Alert correlation before investigation | Prevents 12 investigations for 1 root cause. |

### What to drop, deliberately

Multi-tenancy, RBAC/SSO, the config service, K8s sandbox isolation, Slack/Teams surfaces, 45 integrations, RAPTOR's full tree-building, the MCP codegen pipeline. Every one of these is a legitimate engineering problem and every one of them is a way to spend eight weeks and have no working diagnosis at the end.

**Cut line: if it doesn't make a diagnosis more accurate, more grounded, or more visibly autonomous — it's out of v1.**

---

## 1. Scope

### In scope (v1, must ship)
1. A **target system** you own, instrumented, deployed, and continuously exercised by synthetic traffic.
2. A **fault injection engine** with a declarative catalog carrying ground-truth labels.
3. **Autonomous detection** — SLO burn-rate detectors that open investigations with no human in the loop.
4. **Evidence tools** over logs, metrics, traces, git history, runtime state, and a private doc corpus.
5. A **staged investigation engine** with an explicit hypothesis ledger (not free-running ReAct).
6. A **grounding stack**: citation-bound claims, programmatic citation verification, adversarial critic, calibrated abstention.
7. A **security layer**: secret redaction before context, egress credential broker, doc classification tiers, prompt-injection containment.
8. An **evaluation harness** producing accuracy / hallucination-rate / cost metrics over repeated runs.
9. A **glass-box frontend** showing investigations unfolding live, plus a fault-injection control panel and an eval dashboard.

### Explicitly out of scope
Auto-remediation (propose fixes, never apply them), multi-tenancy, auth beyond a single shared login, mobile, real cloud provider integrations, fine-tuning.

### The one demo that has to work
> Reviewer clicks **"Inject: connection pool exhaustion"**. They do nothing else. Within ~90 seconds the UI shows: an SLO breach detected → an investigation auto-opened → three hypotheses raised → evidence gathered against each → two eliminated with cited counter-evidence → a root cause stated with a commit SHA, a log signature, and a doc reference → a proposed fix. Then they open the eval tab and see this same pipeline scored across 200 prior runs.

Build backwards from that sentence.

---

## 2. System architecture

Seven planes. Keep them as separate deployables so you can demo, test, and fail independently.

```
┌───────────────────────────────────────────────────────────────────────┐
│ PLANE 1 — TARGET SYSTEM (you own this)                                │
│  gateway → orders-svc → payments-svc → worker  |  Postgres, Redis     │
│  structured JSON logs · OTel traces · Prom metrics · load generator   │
└──────────────────────────────┬────────────────────────────────────────┘
                               │ telemetry
┌──────────────────────────────▼────────────────────────────────────────┐
│ PLANE 2 — OBSERVABILITY   Loki · Prometheus · Tempo · Postgres(events) │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
┌──────────────────────────────▼────────────────────────────────────────┐
│ PLANE 3 — DETECTION & TRIGGER                                          │
│  SLO burn-rate detectors → alert dedup/correlation → Investigation     │
│  opened autonomously (no human)                                        │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
┌──────────────────────────────▼────────────────────────────────────────┐
│ PLANE 4 — EVIDENCE LAYER (deterministic tools, no LLM)                 │
│  log_stats · log_sample · metric_query · trace_search · git_diff_since │
│  · runtime_state · doc_search        →  every return = Evidence object │
└──────────────────────────────┬────────────────────────────────────────┘
                               │  ⟂ SECRET REDACTION GATE ⟂
┌──────────────────────────────▼────────────────────────────────────────┐
│ PLANE 5 — INVESTIGATION ENGINE                                         │
│  Triage → Hypothesize → Gather → Verify → Critic → Report              │
│  hypothesis ledger · budget governor · subagent isolation              │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
┌──────────────────────────────▼────────────────────────────────────────┐
│ PLANE 6 — GROUNDING & SCORING                                          │
│  citation validator · claim-evidence matcher · abstention gate         │
│  · eval harness scoring vs ground truth                                │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
┌──────────────────────────────▼────────────────────────────────────────┐
│ PLANE 7 — FRONTEND   live timeline · fault console · eval dashboard    │
└───────────────────────────────────────────────────────────────────────┘

   ⟵ FAULT INJECTION ENGINE writes ground truth to Plane 6, acts on Plane 1 ⟶
```

The critical property: **the fault engine writes the ground-truth label to a store the investigation engine cannot read.** Enforce it at the database-permission level, not by convention. If a reviewer suspects leakage, your entire result is worthless.

---

## 3. Plane 1 — The target system

Don't build a toy. Build the smallest system whose failures are genuinely *ambiguous* — where the symptom does not trivially name the cause.

**Suggested topology:** `gateway` → `orders-svc` → `payments-svc` → `worker` (queue consumer), backed by Postgres and Redis. Python/FastAPI or Node — pick what you write fastest.

**Why this shape:** a slow `payments-svc` surfaces as gateway timeouts. Redis eviction surfaces as Postgres load. Worker backlog surfaces as stale reads. The symptom is always two hops from the cause — which is exactly what makes RCA hard and your agent's work non-trivial.

**Instrumentation requirements (do this on day 1, retrofitting is misery):**
- Structured JSON logs, every line carrying `trace_id`, `span_id`, `service`, `level`, `event`, `commit_sha`.
- OpenTelemetry auto-instrumentation → Tempo.
- RED metrics per service + pool/queue/cache gauges → Prometheus.
- A `deployments` table: `(commit_sha, service, deployed_at, diff_summary)`. This is what makes git correlation possible.

**Load generator:** a `k6` or Locust profile running continuously with a realistic diurnal shape. Without steady baseline traffic there are no anomalies to detect, and your metrics are meaningless.

---

## 4. The fault catalog — your most valuable artifact

This is what makes the project a *system* rather than a wrapper. Faults are declarative, versioned, and self-labeling.

```yaml
- id: FAULT-007
  name: connection_pool_exhaustion
  category: resource
  inject:
    mode: config_patch          # config_patch | git_commit | chaos | traffic
    target: orders-svc
    change: {DB_POOL_MAX: 50 → 3}
  ground_truth:
    root_cause: "orders-svc DB pool reduced to 3; concurrent checkout saturates pool"
    causal_chain:
      - "pool max reduced by config change"
      - "checkout requests queue on pool acquisition"
      - "acquisition timeouts surface as gateway 504s"
    proximate_service: orders-svc
    originating_change: "commit introduced by injector"
    correct_fix_class: "restore pool sizing / add pool telemetry alert"
  expected_signals:
    logs:    ["QueuePool limit .* overflow", "connection acquisition timeout"]
    metrics: ["db_pool_waiters > 10", "http_p99_ms{svc=gateway} > 3000"]
    traces:  ["long span: db.acquire"]
  decoys: [redis_latency_mild]     # concurrent noise that must NOT be blamed
  severity: high
```

**Target ~15 faults across five classes.** Aim for genuine diversity of *reasoning shape*, not just variety of symptoms:

| Class | Examples | What it tests |
|---|---|---|
| Resource | pool exhaustion, memory leak, disk fill | Threshold/saturation reasoning |
| Code regression | N+1 query, unbounded loop, bad serialization — **shipped as a real git commit** | Git-history correlation |
| Config | wrong timeout, bad feature flag, cache TTL of 0 | Change-diff reasoning |
| Dependency | downstream latency, Redis eviction storm, partial network fault | Multi-hop causal tracing |
| Data | schema drift, poison message, clock skew | Cross-signal correlation |

**Two design details that matter enormously:**

1. **Ship code-regression faults as actual git commits** to the target repo, then deploy. Now `git_diff_since(t)` returns a real diff the agent must correlate. Faked git history is instantly obvious to a technical reviewer.
2. **Include decoys.** Run mild background anomalies unrelated to the fault. An agent that blames the decoy is hallucinating causality from correlation — and your eval will catch it. This is the single most convincing thing in your eval suite.

---

## 5. Plane 3 — Autonomy

"Autonomous" must mean something specific and observable, not "we call it on a cron."

**Detection: multi-window SLO burn rate** (the Google SRE approach), not static thresholds. Fast burn (2% budget in 1h) → page-equivalent. Slow burn (5% in 6h) → ticket-equivalent. This is defensible engineering with a citation behind it, and it's dramatically better than `if error_rate > 0.05`.

**Correlation before investigation.** One fault fires many alerts. Group by: temporal proximity (±2 min) + topology adjacency (service dependency graph) + signal similarity. Emit **one** `Incident` per cluster. Report your noise-reduction ratio — it's a real number you can show.

**The autonomy loop:**
```
detector fires → correlate → dedupe against open incidents
  → create Incident (status=OPEN)
  → enqueue Investigation
  → engine picks up, runs to completion
  → verdict published, incident annotated
  → if unresolved after N min, re-investigate with widened scope
```
Everything after `detector fires` runs with zero human input. The frontend observes; it does not drive.

---

## 6. Plane 4 — Evidence layer

Every tool is **deterministic, non-LLM, and returns a typed `Evidence` object.** The LLM never sees raw untyped blobs.

```python
@dataclass(frozen=True)
class Evidence:
    id: str              # "ev_a3f9" — stable, hashed, citable
    kind: Literal["log","metric","trace","git","state","doc"]
    query: str           # exact query executed — reproducible
    time_range: tuple[datetime, datetime]
    content: str         # REDACTED payload
    raw_hash: str        # hash of pre-redaction content, for audit
    source_ref: str      # loki stream / commit sha / doc chunk id
    retrieved_at: datetime
```

**The tool set (keep it small — 7 tools, not 45):**

| Tool | Contract |
|---|---|
| `log_stats(service, window, group_by)` | **Always call before sampling.** Returns error-rate deltas, top templates by frequency, novel-pattern list. |
| `log_sample(pattern, window, k≤50)` | Targeted retrieval only. Hard cap. |
| `metric_query(promql, window)` | Returns downsampled series + computed delta vs. 24h-prior baseline. |
| `trace_search(filters)` | Exemplar slow traces with span breakdown. |
| `git_diff_since(timestamp, services)` | Commits + diffs deployed in window. |
| `runtime_state(service)` | Container status, restarts, resource usage, config hash. |
| `doc_search(query, tier)` | RAG over the private corpus, tier-gated. |

**Log volume is the thing that will kill you.** The statistics-first pattern from the reference repo is the answer: cluster log lines into templates (Drain3 algorithm), show the agent *"template X: 4,200 occurrences, up 340% vs baseline; template Y: 12 occurrences, never seen before"*, and let it request samples of specific templates. Novel-template detection alone catches a surprising share of incidents.

**RAG over company docs:** don't build RAPTOR. Do hybrid retrieval — BM25 + dense embeddings (`text-embedding-3-small`) with reciprocal-rank fusion, over runbooks, architecture docs, ADRs, and past postmortems. Chunk with heading-aware splitting; every chunk keeps `doc_id`, `heading_path`, `classification_tier`. Fusion retrieval beats pure-dense reliably and takes an afternoon.

---

## 7. Plane 5 — The investigation engine

**Do not build an open-ended ReAct loop.** Unbounded loops are where agentic demos go to die — they wander, blow budget, and produce confident nonsense. Build a **staged state machine with a hypothesis ledger.**

### The ledger

```python
@dataclass
class Hypothesis:
    id: str
    statement: str                       # "orders-svc DB pool exhausted"
    predicted_signals: list[str]         # what MUST be true if this holds
    supporting: list[EvidenceRef]
    contradicting: list[EvidenceRef]
    status: Literal["open","supported","refuted","insufficient"]
    posterior: float                     # updated each round
```

Forcing the model to state **predicted_signals before gathering evidence** is the highest-leverage anti-hallucination mechanism in the whole design. It converts the agent from a narrator (which will always find a story) into a falsifier. If the predicted signal is absent, the hypothesis is refuted — mechanically, not by vibes.

### The stages

| Stage | Model | Purpose | Exit condition |
|---|---|---|---|
| **1. Triage** | `gpt-4.1-mini` | Classify incident, identify blast radius, pick candidate services | Scope defined |
| **2. Hypothesize** | `gpt-4.1` / o-series | Generate 3–5 competing hypotheses **with falsifiable predictions**. Seeded by doc-RAG + past similar incidents | ≥3 hypotheses with predictions |
| **3. Gather** | `mini` (parallel subagents) | One isolated subagent per hypothesis; each returns structured findings only | All hypotheses tested or budget hit |
| **4. Verify** | `gpt-4.1` | Update posteriors from evidence. Refute what's refuted | One dominant or all weak |
| **5. Critic** | separate prompt, fresh context | Adversarial: "here is the conclusion and its evidence. Argue it's wrong. Name a better explanation." | Critic verdict recorded |
| **6. Report** | `gpt-4.1` | Causal chain, cited claims, proposed fix, confidence | Passes validator |

**Budget governor:** hard caps on tokens, tool calls, wall-clock, and dollars per investigation. Exceeding budget → `INSUFFICIENT_EVIDENCE` with partial findings, never a guess. Log every investigation's cost and show it in the UI — cost-awareness reads as production maturity.

**Subagent isolation:** each Gather subagent gets only its own hypothesis and tool access, returns a compact structured finding. The main context never sees raw evidence dumps. This is the difference between an agent that works at 5 hypotheses and one that collapses at 2.

---

## 8. Plane 6 — The grounding stack

Five layers. Layers 1–3 are mechanical, and that's the point: **do not use an LLM to check an LLM as your primary defense.**

**Layer 1 — Structured output, always.** Every stage returns a JSON schema (use OpenAI structured outputs). No free-text parsing.

**Layer 2 — Citation binding.** Every claim in the report carries `evidence_ids`. Schema-enforced:
```json
{"claim": "DB pool saturated at 14:32", "evidence_ids": ["ev_a3f9","ev_c21b"], "confidence": 0.87}
```

**Layer 3 — Programmatic citation verification.** This is your headline guardrail. For every claim:
- Do all cited evidence IDs exist in this investigation's evidence store? *(catches fabricated citations)*
- Does the cited evidence's time range contain the claimed timestamp? *(catches temporal fabrication)*
- Do the numbers in the claim appear in the cited evidence, within tolerance? *(catches invented figures)*
- Are entity names in the claim present in the cited evidence? *(catches invented services/commits)*

Any claim failing verification is **stripped or flagged**, and the investigation is marked `PARTIALLY_VERIFIED`. Log the strip-rate — "3.2% of generated claims failed citation verification and were removed" is a killer line in a demo.

**Layer 4 — Adversarial critic.** Fresh context, sees only conclusion + evidence, instructed to falsify. Disagreement caps final confidence.

**Layer 5 — Calibrated abstention.** Explicit outcome states:
```
CONFIRMED (≥0.80, critic concurs, all claims verified)
LIKELY (0.55–0.80)
INSUFFICIENT_EVIDENCE (<0.55, or budget exhausted, or critic dissents)
```
`INSUFFICIENT_EVIDENCE` is a **success state**, and it must be reachable in your demo. Include 2–3 "unfalsifiable" faults in the catalog specifically to prove the system abstains. A system that never says *I don't know* is a system that hallucinates — and any competent reviewer knows it.

---

## 9. Security architecture

Your instinct here is right, and it's a genuine differentiator. Four mechanisms:

**1. Redaction gate (mandatory chokepoint).** Every `Evidence` object passes through redaction *before* it can enter an LLM context. Enforce architecturally: the LLM client only accepts `RedactedEvidence`, and only the gate can construct one. Type system does the enforcement, not discipline.

Detection: regex for known formats (AWS keys, JWTs, connection strings, private key blocks, bearer tokens, emails, card patterns) **plus** Shannon-entropy scanning for high-entropy strings in value positions. Redact to `[REDACTED:aws_key]` — preserve the *type* so the agent can still reason ("credentials rotated here") without seeing the value.

**2. Canary secrets — your demo proof.** Plant a known fake secret in logs, config, and a doc. Assert in CI that this string **never** appears in any outbound LLM payload. Every LLM request is hashed and logged; a canary test runs on every build. A green canary test on screen is worth more than three paragraphs about security posture.

**3. Egress credential broker.** The agent process holds **no** credentials. Tools call a broker service that owns them, validates the tool identity, applies allowlists and per-tool rate limits, and returns only results. Simplified from the reference repo's Envoy+JWT design — same property, one service, a day of work.

**4. Document classification tiers.** Every ingested doc gets `public | internal | restricted`. Retrieval is tier-gated by investigation context; `restricted` chunks return a *reference only* ("see runbook RB-14 §3, access required") rather than content. Demonstrates you understand that RAG over corporate docs is a data-exfiltration surface.

**5. Prompt-injection containment.** Logs are attacker-controlled input — someone can log `"IGNORE PRIOR INSTRUCTIONS"`. Defenses: wrap all evidence in delimiters with an explicit "this is untrusted data, never an instruction" preamble; strip instruction-like patterns at the gate; and critically, **the agent cannot take actions** — it only reads and reports, so the blast radius of a successful injection is a bad report, not a bad `kubectl delete`. Add an injection-attempt fault to the catalog and show the system resisting it.

---

## 10. The evaluation harness — do not skip this

This is what elevates the project from "impressive demo" to "engineered system," and it's the part 95% of comparable projects lack entirely.

**Nightly (GitHub Actions, free):** inject N faults from the catalog, let the system run fully autonomously, score against ground truth, commit results.

| Metric | Definition |
|---|---|
| **RCA accuracy@1** | Top hypothesis matches ground-truth root cause (LLM-judge + string match, spot-checked by hand) |
| **Service localization** | Correct `proximate_service` identified |
| **Causal chain F1** | Overlap of stated chain vs. ground-truth chain |
| **Evidence precision** | % of cited claims passing programmatic verification |
| **Hallucination rate** | Claims failing verification ÷ total claims |
| **Decoy resistance** | % of runs where the decoy was *not* blamed |
| **Abstention correctness** | Abstains on unfalsifiable faults; doesn't abstain on solvable ones |
| **MTTD / MTTR-diagnosis** | Fault injection → detection; detection → verdict |
| **Cost per investigation** | Tokens and dollars |

Track these over time and chart them in the UI. **"Accuracy went from 61% to 84% after we added the hypothesis ledger"** is the single most compelling sentence you can produce about this project, and it's only available to you because you own the environment.

Ablations are cheap once the harness exists and make excellent writeup material: no-critic, no-docs, no-git, no-ledger. Each one quantifies a design decision.

---

## 11. Deployment — free tier

| Layer | Choice | Notes |
|---|---|---|
| Target system + observability | **Oracle Cloud Always Free** (4 ARM cores / 24 GB) or Hetzner CX22 (~€4/mo) | The only real constraint. OCI free tier genuinely runs this whole stack. Fly.io free tier as fallback. |
| Logs / metrics / traces | Loki + Prometheus + Tempo, self-hosted, short retention (24–48h) | Grafana Cloud free tier as backup |
| App DB + vectors | Postgres + `pgvector` | One database. Don't add a vector DB. |
| Queue | Postgres `SKIP LOCKED` or Redis | Don't add Kafka. |
| Backend / engine | FastAPI in Docker Compose on the same VM | |
| Frontend | Next.js on **Vercel free** | |
| CI + nightly evals | **GitHub Actions** (2000 min/mo free) | |
| LLM | OpenAI: `gpt-4.1-mini` for triage/gather, `gpt-4.1` or `o4-mini` for hypothesize/verify/critic | Route by stage. Budget ~$40–70 total; cache aggressively; use `mini` for everything that isn't reasoning. |

Verify current OpenAI model names and pricing when you start — they change frequently.

**Cost control:** cache evidence-retrieval results by `(query, time_range)`; run evals against a recorded-fixture mode so you're not paying for LLM calls on every CI run; enforce the budget governor from week 1.

---

## 12. Eight-week plan

**Principle: get a thin vertical slice working end-to-end by week 3.** One fault, one detector, one hypothesis, one report, visible in the UI. Everything after that is deepening. Teams that build horizontally — all the tools, then all the agent, then the UI — ship nothing.

| Week | Deliverable | Done when |
|---|---|---|
| **0** (2 days) | Scope locked, fault catalog v1 drafted, repos + skeleton up | This doc is signed off and 8 faults are specified |
| **1** | Target system running, instrumented, load generator live, telemetry flowing | You can query Loki/Prom/Tempo for real traffic |
| **2** | Fault engine + 5 faults working; ground-truth store isolated; evidence tools built | `inject FAULT-007` visibly degrades the system |
| **3** | **Vertical slice:** detector → investigation → single-hypothesis report → UI | Reviewer sees one fault auto-diagnosed end to end |
| **4** | Full staged engine: ledger, subagents, critic, budget governor | 5 hypotheses handled without context collapse |
| **5** | Grounding stack: citation binding, programmatic verification, abstention | Hallucination rate is a number you can quote |
| **6** | Doc RAG + git correlation + security layer (redaction, canary, broker, tiers) | Canary test green in CI; diagnoses cite commits and docs |
| **7** | Eval harness + nightly runs + full 15-fault catalog + ablations | Accuracy chart with a real trend line |
| **8** | Frontend polish, deployment hardening, demo script, writeup/README | Cold-start demo runs clean twice in a row |

**Buffer strategy:** week 7 is where things slip. If you're behind entering week 6, cut the fault catalog to 10 and the ablations to two — never cut the eval harness or the grounding stack, since they're the actual thesis.

---

## 13. Frontend — the glass box

Not a chat window. Everyone builds a chat window. Build an **observation deck**:

1. **Live investigation view** — the money screen. Timeline streaming over WebSocket/SSE: alert fired → hypotheses posted with confidence bars → evidence arriving and attaching to hypotheses → bars moving as posteriors update → hypotheses greying out as refuted → final verdict with every claim expandable to show its cited evidence. Watching confidence shift in real time makes the reasoning legible in a way no transcript can.
2. **Fault console** — buttons to inject any catalog fault, plus "surprise me" (random, hidden from viewer). Being able to hand a reviewer the controls is a strong signal of confidence.
3. **Eval dashboard** — accuracy over time, per-fault-class breakdown, hallucination rate, decoy resistance, cost per investigation.
4. **Evidence inspector** — click any citation, see the exact query, time range, and redacted payload. This is your grounding claim made *inspectable* rather than asserted.

Next.js + Tailwind + Recharts + SSE. Keep it dark, dense, and technical — instrument-panel aesthetic, not SaaS-landing-page.

---

## 14. Risk register

| Risk | Mitigation |
|---|---|
| **Log volume blows context** | Statistics-first + Drain3 templating from day 1; hard sample caps |
| **Agent wanders, burns budget** | Staged state machine, not open ReAct; budget governor with hard kill |
| **Ground truth leaks into investigation** | DB-level permission separation; add a test that asserts isolation |
| **Faults too easy → 100% accuracy looks fake** | Decoys, multi-hop faults, and 2–3 deliberately unfalsifiable cases |
| **OCI free tier can't handle the load** | Keep retention at 24h; scale load generator down; Hetzner is €4/mo |
| **Week 7 slip** | Cut catalog size and ablations first; never cut eval or grounding |
| **Non-determinism makes evals noisy** | temperature=0 where possible; run each fault 3× and report mean ± spread |

---

## 15. Your 48-hour decision list

Lock these before writing code. Everything else can be discovered.

1. **Target system domain and topology** — 4 services, what do they do? *(2 hrs)*
2. **Fault catalog v1** — write 8 faults in full YAML with ground truth. This forces you to think about what "root cause" even means. *(4 hrs)*
3. **The `Evidence` schema and the redaction gate boundary** — hardest thing to change later. *(2 hrs)*
4. **Hypothesis ledger schema and stage contracts** — exact JSON in/out per stage. *(3 hrs)*
5. **Scoring definitions** — how exactly do you decide an RCA was "correct"? Write it down *before* you have results, or you will unconsciously grade generously. *(2 hrs)*
6. **Deployment target confirmed** — actually provision the OCI/Hetzner box and SSH into it. Don't assume it'll work in week 8. *(1 hr)*
7. **Repo structure + CI skeleton** *(2 hrs)*

---

## 16. What makes this defensible

If asked "isn't this just a wrapper around GPT?", the answers are:

- **The fault injection engine and ground-truth contract** — a testbed, not a prompt.
- **The hypothesis ledger with pre-registered falsifiable predictions** — a reasoning protocol that mechanically refutes, rather than a model asked to think step by step.
- **Programmatic citation verification** — deterministic code that catches the model lying, with a measured strip-rate.
- **The evaluation harness** — quantified accuracy, hallucination rate, and decoy resistance, tracked over time with ablations.
- **The security architecture** — canary-tested redaction, credential brokering, tiered document access.
- **Calibrated abstention** — the system knows when it doesn't know.

The LLM is one component inside a system that constrains it, verifies it, and measures it. That's the difference, and it's worth saying explicitly in your README.
