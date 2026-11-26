# sherLOG v1 — Locked Scope: Target App, Fault Catalog, Monitoring

Companion to `ARCHITECTURE.md`. That document defines the *system*; this one locks the three
things that must be pinned before any code is written, per §15 items 1–2 and §11.

**Governing principle for v1:** every element here must be (a) free, (b) buildable by us in the
week allotted, and (c) *generalisable* — nothing may be hardcoded to this app in a way that blocks
pointing sherLOG at a different system later. Where v1 takes a shortcut, it is named explicitly
under **Generalisation seam** so we know what to rip out in v2.

---



## Part A — The target application



### A.1 Decision

**"Nimbus Store" — a minimal e-commerce checkout path.** Five processes, two datastores, one
chaos proxy. Python 3.12 + FastAPI throughout, one shared `nimbus_common` package for logging,
tracing, metrics, and config. Estimated **~800 lines of application code total**.

Domain chosen because checkout is the one flow every reviewer already understands, so no
explanation budget is spent on the app itself — all of it goes to the diagnosis.

### A.2 Topology

```
        k6 load generator
               │
               ▼
        ┌─────────────┐
        │  gateway    │  :8000   public HTTP, owns timeouts + retries, no DB
        └──────┬──────┘
               │ HTTP
        ┌──────▼──────┐         ┌─────────┐
        │ orders-svc  │  :8001 ─┤  Redis  │  product/price cache
        └──┬───────┬──┘         └─────────┘
           │       │
           │       └──────────────┐
       ┌───▼────────┐             │ HTTP
       │ Postgres   │◄────────┐   │
       │ orders,    │         │ ┌─▼────────────┐
       │ items,     │         │ │ payments-svc │ :8002
       │ inventory, │         │ └─┬────────────┘
       │ payments,  │         │   │ HTTP
       │ jobs,      │         │ ┌─▼────────────┐
       │ deployments│         │ │  toxiproxy   │  ← chaos injection point
       └────────────┘         │ └─┬────────────┘
           ▲                  │   │
           │            ┌─────┴─┐ │ ┌──────────┐
           └────────────┤worker │ └─►  psp-stub│  fake external payment provider
                        └───────┘   └──────────┘
```



### A.3 Service contracts


| Service        | Port | Responsibility                                                                                                                                                           | Owns                        |
| -------------- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------- |
| `gateway`      | 8000 | `POST /checkout`, `GET /orders/{id}`, `GET /healthz`. Sets the client timeout and retry policy for calls to orders-svc. Stateless.                                       | HTTP client config          |
| `orders-svc`   | 8001 | `POST /orders`, `GET /orders/{id}`. Reads catalog+price from Redis (fallback Postgres), writes `orders`/`order_items`, calls payments-svc, enqueues a `fulfillment` job. | Postgres pool, Redis client |
| `payments-svc` | 8002 | `POST /payments` with idempotency key. Calls `psp-stub` through Toxiproxy. Writes `payments`.                                                                            | Upstream HTTP client        |
| `worker`       | —    | Polls `jobs` with `FOR UPDATE SKIP LOCKED`, decrements inventory, marks order `COMPLETE`, retries with exponential backoff, dead-letters after N attempts.               | Job loop                    |
| `psp-stub`     | 8003 | ~30 lines. Returns `{"status":"captured"}` after a normal 40–80 ms. Stands in for Stripe.                                                                                | Nothing                     |


Datastores: **Postgres 16** (application data + `deployments` + `jobs` queue + ground-truth store
in a *separate database with separate role*), **Redis 7** (catalog cache).

### A.4 Why this topology produces ambiguous failures

This is the whole reason for the shape. For each fault, symptom and cause are on different services:


| Cause lives in            | Symptom appears on                                                   | Hops                                  |
| ------------------------- | -------------------------------------------------------------------- | ------------------------------------- |
| orders-svc DB pool config | gateway 504s                                                         | 2                                     |
| psp-stub latency          | gateway timeouts                                                     | 3                                     |
| Redis cache config        | Postgres CPU, then orders latency, then gateway p99                  | 3                                     |
| A malformed order row     | `GET /orders/{id}` returns stale `PENDING`, **zero errors anywhere** | 2, and invisible to error-rate SLOs   |
| An orders-svc code commit | latency only in the large-basket traffic cohort                      | 1, but only under a traffic condition |


The last two matter most. The poison-message fault breaches a *freshness* SLO while availability
and latency stay green — an agent that only knows how to read error rates will fail it outright.
The N+1 fault is invisible in aggregate p99 and only shows up when you segment by basket size.

### A.5 Traffic profile (k6, runs continuously)

- **Baseline: 10 requests/sec** to `POST /checkout`, plus 3 rps to `GET /orders/{id}`.
- Basket-size mix: **80% small (1–3 items), 20% large (15–30 items)** — tagged in k6 as
`cohort=small|large` and carried into a request header the services log. This cohort split is
what makes the N+1 fault detectable and non-trivial.
- Diurnal shape: a sine multiplier between 0.6× and 1.6× over a 30-minute synthetic "day", so
baselines are non-flat and naive threshold detection visibly fails where burn-rate detection works.
- 2% of requests use a deliberately invalid payment method → a **steady non-zero error floor**.
Without a floor, any error is trivially the incident; with one, the agent has to reason about deltas.

**Sizing check:** 13 rps × ~6 log lines/request ≈ 78 lines/s ≈ 6.7 M lines/day, roughly 1.5 GB raw
before compression. Comfortable for Loki at 24 h retention on the free-tier box.

### A.6 What we are NOT building in v1

No auth, no real payments, no frontend for the store itself, no inventory reservations, no
multi-region, no Kubernetes. The store is a *fault substrate*, not a product.

> **Generalisation seam:** services must emit telemetry through `nimbus_common` only, and sherLOG
> must read *only* Loki/Prometheus/Tempo/Postgres — never import from `nimbus_common` or query app
> tables directly. If that boundary holds, pointing v2 at a different system is a config change.

---



## Part B — The fault catalog



### B.1 Decision: 6 injectable faults + 2 decoys + 1 abstention case

`ARCHITECTURE.md` targets ~15 faults. **For v1 we cut to 6.** Rationale: fault count buys
*eval sample size*, not *system capability*, and sample size is worthless until the pipeline is
correct. Six faults across four reasoning shapes proves every mechanism in the design; faults
7–15 are then a data-entry exercise we can do in week 7 if time exists.

The six are chosen so that **two pairs are mutually confusable**, which is where the actual
scientific content of the eval lives.

### B.2 Injection mechanisms (all four are real, none are simulated)


| Mechanism             | How                                                                                                                                                                                                                                                               | Used by               |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| **M1** `config_patch` | Injector rewrites the service's env file and runs `docker compose up -d --force-recreate <svc>`. Real restart, real config hash change, visible in `runtime_state`.                                                                                               | F-01, F-04, F-05      |
| **M2** `git_commit`   | Injector applies a pre-authored patch file to the target repo, commits with a plausible message and author, rebuilds the image, redeploys, and writes a row to `deployments(commit_sha, service, deployed_at, diff_summary)`. **A real commit with a real diff.** | F-03, F-06            |
| **M3** `chaos`        | Toxiproxy API call adds a `latency`/`bandwidth`/`timeout` toxic on the payments→psp-stub link.                                                                                                                                                                    | F-02                  |
| **M4** `data`         | Injector inserts a crafted row / enqueues a crafted job directly.                                                                                                                                                                                                 | F-06 alternative path |


All four are free and self-hosted. No fault is implemented as `if FAULT_FLAG: sleep()` inside
application code — that shortcut would make git correlation and config-diff reasoning fraudulent,
and a technical reviewer spots it immediately.

### B.3 The six faults



#### Pair 1 — *cache degradation*: same symptom shape, different cause

**F-01** `cache_ttl_collapse` (config)

- Inject: `orders-svc` env `CATALOG_CACHE_TTL_S: 300 → 0`, restart.
- Chain: TTL 0 → every catalog read misses → Postgres read QPS ~8× → orders p99 climbs → gateway p99 breaches latency SLO. **No errors.**
- Discriminating signal: `cache_ops_total{result="miss"}` rises while `redis_evicted_keys_total` stays **flat** and `redis_memory_used_bytes` is normal. Config hash for orders-svc changed.
- Class: config / threshold-free reasoning.

**F-02** `redis_eviction_storm` (config)

- Inject: Redis `maxmemory: 256mb → 4mb` (v1: 8mb measured just under the fast-burn threshold), restart Redis.
- Chain: keys evicted under memory pressure → cache miss rate rises → identical downstream shape to F-01.
- Discriminating signal: `redis_evicted_keys_total` **rising steeply**, `redis_memory_used_bytes` pinned at the limit, and orders-svc config hash **unchanged**.
- Class: dependency / multi-hop.

> These two are near-indistinguishable at the gateway. Getting them right requires the agent to
> reach for the *discriminating* signal rather than the *confirming* one — which is exactly what
> the pre-registered `predicted_signals` field in the hypothesis ledger is designed to force.
> This pair alone justifies the ledger's existence in the writeup.



#### Pair 2 — *saturation*: same symptom, one config, one external

**F-03** `db_pool_exhaustion` (config)

- Inject: `orders-svc` env `DB_POOL_MAX: 20 → 3`, restart.
- Chain: pool starved → `db_pool_waiters` grows → acquire timeouts → 500s from orders → gateway 504s → availability SLO burns fast.
- Signals: log event `db.acquire.timeout`, `db_pool_waiters > 10`, trace spans dominated by `db.acquire`, config hash changed.
- Class: resource / saturation.

**F-04** `psp_latency_injection` (chaos)

- Inject: Toxiproxy `latency` toxic, 1500 ms ± 300 jitter, on `payments→psp` link.
- Chain: psp slow → payments-svc request duration ↑ → orders-svc blocks on payments → orders' own pool saturates as a *secondary* effect → gateway 504s.
- **The trap:** this fault produces `db_pool_waiters` elevation too, because blocked handlers hold connections. An agent that stops at "pool exhausted" gets the *proximate* service right and the *root cause* wrong. Ground truth records both, and we score them separately.
- Discriminating signal: `upstream_duration_seconds{upstream="psp"}` p99 ≫ baseline, and pool config unchanged. Traces show the time inside the psp span, not the db.acquire span.
- Class: dependency / multi-hop causal tracing.



#### Two singletons — different reasoning shapes entirely

**F-05** `n_plus_one_regression` (git commit)

- Inject: a real commit to `orders-svc` replacing a single joined catalog query with a per-item loop. Message: `perf: simplify item enrichment logic`. Rebuild, redeploy, record in `deployments`.
- Chain: query count scales with basket size → only the `cohort=large` 20% of traffic degrades → aggregate p99 creeps just past the SLO while p50 is untouched.
- Requires: `git_diff_since()` correlation **and** segmenting metrics by the cohort label. This is the fault that proves git correlation is real.
- Decoy interaction: **always injected alongside D-2** (a benign commit deployed in the same window), so the agent must pick the right commit, not just the right time window.
- Class: code regression.

**F-06** `poison_message_backlog` (data + git)

- Inject: enqueue a fulfillment job referencing a SKU with a NULL price, which the worker's handler crashes on and retries forever without dead-lettering (the missing dead-letter path is shipped as a real commit, so this fault has both a data trigger and a code root cause — ground truth names the code).
- Chain: worker retry-loops on one job → head-of-line blocking → `queue_depth` and `job_age_seconds` climb → orders never reach `COMPLETE` → **freshness SLO breaches while availability and latency stay green.**
- Signals: `job_attempts_total{status="retry"}` on a single `job_id`, novel log template (Drain3 should flag it as never-before-seen), `queue_depth` monotonic rise.
- Class: data / cross-signal correlation, and the only fault that does not touch the request path.



### B.4 Decoys (background noise that must never be blamed)


| ID                           | What                                                                                    | Runs                                   |
| ---------------------------- | --------------------------------------------------------------------------------------- | -------------------------------------- |
| **D-1** `mild_redis_latency` | Toxiproxy 15 ms latency toxic on the Redis link — real, measurable, harmless.           | Randomly during ~50% of injections     |
| **D-2** `benign_deploy`      | A real commit + redeploy in the same window that only changes log strings and a README. | Always during F-05; randomly otherwise |


D-2 is the more valuable of the two: "there was a deploy right before the incident" is the single
most seductive false correlation in real SRE work, and an agent that blames it is doing exactly
what a junior on-call does at 3 a.m.

### B.5 The abstention case

**F-07** `observability_blind_spot` (config)

- Inject: stop the Alloy log-shipping container for `payments-svc`, *then* apply a mild psp latency toxic (400 ms).
- Result: metrics show payments-svc degraded, but there are **no logs and no traces** from the window to establish why.
- **Correct outcome:** `INSUFFICIENT_EVIDENCE`, with a stated partial finding ("payments-svc is the proximate service; log evidence unavailable for the window") — not a guess.
- This is a *success state* and it must be in the demo. It is also the only fault where a confident answer is scored as a failure.



### B.6 Ground-truth record (one YAML per fault, exact schema)

```yaml
- id: F-04
  name: psp_latency_injection
  class: dependency
  inject:
    mode: chaos
    target: toxiproxy/payments_to_psp
    change: {toxic: latency, latency_ms: 1500, jitter_ms: 300}
    revert: {remove_toxic: latency}
  ground_truth:
    root_cause: "External payment provider (psp-stub) response latency rose to ~1.5s, saturating orders-svc handlers waiting on payments-svc."
    proximate_service: payments-svc      # where it hurts first
    originating_component: psp-stub      # where it starts
    causal_chain:
      - "psp-stub p99 latency 1500ms (baseline 80ms)"
      - "payments-svc upstream call duration rises proportionally"
      - "orders-svc handlers block on payments, holding DB connections"
      - "orders-svc pool saturates as a secondary effect"
      - "gateway returns 504 on checkout"
    correct_fix_class: "reduce payments client timeout + circuit-break psp; do not resize pool"
    known_misdiagnosis: "db_pool_exhaustion"   # scored explicitly
  expected_signals:
    metrics: ['upstream_duration_seconds{upstream="psp"} p99 > 1.2',
              'db_pool_waiters > 5']
    logs:    ["upstream.timeout", "payments.slow"]
    traces:  ["span psp.capture duration > 1200ms"]
  decoys: [D-1]
  severity: high
  expected_outcome: CONFIRMED
```

The `known_misdiagnosis` field is new and worth keeping: it turns "wrong answer" into
"wrong answer *in the specific way we predicted*", which is a far more informative eval metric
than a binary.

### B.7 Isolation of ground truth

Ground truth lives in Postgres database `sherlog_truth`, owned by role `injector`. The
investigation engine connects as role `agent`, which has **no grant on that database at all**.
A CI test asserts `agent` receives a permission error when it attempts to read it. Convention is
not enough here — if a reviewer suspects leakage, every accuracy number we report is void.

---



## Part C — Monitoring



### C.1 The stack (every component free and self-hosted)


| Signal          | Tool                                                                     | Config                               | Why this one                                                              |
| --------------- | ------------------------------------------------------------------------ | ------------------------------------ | ------------------------------------------------------------------------- |
| Logs            | **Loki** (single-binary, filesystem)                                     | 24 h retention                       | Label-based, cheap, and its aggregation API is what `log_stats` needs     |
| Log shipping    | **Grafana Alloy** (Promtail's successor)                                 | Docker service-discovery, JSON stage | Reads container stdout, no app-side agent                                 |
| Metrics         | **Prometheus**                                                           | 10 s scrape, 48 h retention          | Burn-rate detection needs recording rules, which is Prometheus' home turf |
| Traces          | **Tempo** (single-binary, filesystem)                                    | 24 h, 100% sampling at 13 rps        | Trace-by-ID lookup from a log line's `trace_id` is the killer path        |
| Instrumentation | **OpenTelemetry** SDK + FastAPI/psycopg/redis/httpx auto-instrumentation | OTLP gRPC → Tempo                    | Free, and gives us the cross-service span linkage for nothing             |
| DB internals    | `postgres_exporter`, `redis_exporter`                                    | scraped by Prometheus                | Supplies the F-01/F-02 discriminator                                      |
| Container state | **cAdvisor**                                                             | scraped by Prometheus                | Restart counts, OOM kills, CPU/mem per container → backs `runtime_state`  |
| Load            | **k6** (OSS, self-hosted)                                                | continuous, profile in §A.5          | Lighter than Locust; scenarios are code                                   |
| Chaos           | **Toxiproxy** (MIT)                                                      | API-driven toxics                    | The only free tool that does clean L4 fault injection                     |
| Our debugging   | **Grafana**                                                              | dashboards                           | For *us*, not part of the product — sherLOG must never query Grafana      |


**Cost: $0.** All of it runs in Docker Compose on one Oracle Cloud Always Free ARM instance
(4 OCPU / 24 GB). Rough memory budget: Loki 1.0 G, Prometheus 1.5 G, Tempo 1.0 G, Postgres 1.0 G,
Redis 0.3 G, 5 app services 1.5 G, exporters + cAdvisor + Alloy 0.6 G, k6 0.3 G, sherLOG engine
1.5 G ≈ **9 GB**, leaving generous headroom.

> **Verify before committing to ARM:** confirm `arm64` images exist for Toxiproxy and
> `postgres_exporter`. Both are Go and trivially buildable from source if not, but find out in
> week 0, not week 8.



### C.2 Log schema (fixed, enforced in `nimbus_common`)

```json
{
  "ts": "2026-08-15T14:32:07.412Z",
  "level": "ERROR",
  "service": "orders-svc",
  "commit_sha": "a3f9c21",
  "config_hash": "7d21e0",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "00f067aa0ba902b7",
  "event": "db.acquire.timeout",
  "route": "POST /orders",
  "cohort": "large",
  "duration_ms": 3021,
  "attrs": {"pool_size": 3, "waiters": 14},
  "msg": "connection acquisition timed out after 3000ms"
}
```

Two rules that everything downstream depends on:

1. `event` **is a stable dotted enum**, never an interpolated string. Variable data goes in
  `attrs`. This makes Drain3 templating near-perfect instead of merely good, and makes
   "novel template never seen before" a trustworthy signal rather than a noise generator.
2. `commit_sha` **and** `config_hash` **are on every single line.** These are what let the agent
  correlate a log to a deploy without a join, and they are why F-05 is solvable at all.

Loki labels: `service`, `level` only. Everything else stays in the JSON body and is extracted at
query time — high-cardinality labels are the standard way people destroy a Loki install.

### C.3 Metrics (exact names)


| Metric                                                          | Type          | Labels                        | Feeds                          |
| --------------------------------------------------------------- | ------------- | ----------------------------- | ------------------------------ |
| `http_requests_total`                                           | counter       | `service,route,status,cohort` | availability SLI               |
| `http_request_duration_seconds`                                 | histogram     | `service,route,cohort`        | latency SLI, F-05 cohort split |
| `upstream_requests_total`                                       | counter       | `service,upstream,status`     | F-04                           |
| `upstream_duration_seconds`                                     | histogram     | `service,upstream`            | **F-04 discriminator**         |
| `db_pool_size` / `db_pool_in_use` / `db_pool_waiters`           | gauge         | `service`                     | F-03                           |
| `db_pool_acquire_seconds`                                       | histogram     | `service`                     | F-03                           |
| `cache_ops_total`                                               | counter       | `service,op,result`           | F-01, F-02                     |
| `queue_depth`                                                   | gauge         | `queue`                       | F-06                           |
| `job_oldest_age_seconds`                                        | gauge         | `queue`                       | **freshness SLI**              |
| `job_attempts_total`                                            | counter       | `queue,status`                | F-06                           |
| `order_completion_seconds`                                      | histogram     | —                             | freshness SLI                  |
| `redis_evicted_keys_total`, `redis_memory_used_bytes`           | from exporter | —                             | **F-01 vs F-02 discriminator** |
| `pg_stat_database_tup_fetched`, `pg_stat_activity_count`        | from exporter | `datname`                     | F-01, F-02                     |
| `container_memory_working_set_bytes`, `container_restart_count` | cAdvisor      | `name`                        | `runtime_state`                |




### C.4 SLOs and the detection rule

Three SLIs, deliberately chosen so that **each fault breaches a different one**:


| SLO              | SLI definition                                                                                                                                                  | Target | Breached by      |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ | ---------------- |
| **Availability** | `sum(rate(http_requests_total{service="gateway",route="/checkout",status!~"5.."}[w])) / sum(rate(http_requests_total{service="gateway",route="/checkout"}[w]))` | 99.0%  | F-03, F-04       |
| **Latency**      | fraction of `/checkout` requests under 400 ms (2x healthy p99), from the histogram                                                                                             | 99.0%  | F-01, F-02, F-05 |
| **Freshness**    | fraction of orders reaching `COMPLETE` within 60 s                                                                                                              | 99.0%  | **F-06 only**    |


**Detection: multi-window, multi-burn-rate**, per the Google SRE workbook — with windows
compressed for demo tempo.


| Alert     | Long window | Short window | Burn rate | Budget consumed    | Fires within |
| --------- | ----------- | ------------ | --------- | ------------------ | ------------ |
| Fast burn | 5 min       | 30 s         | 14.4×     | 2% of a 6 h budget | ~45–75 s     |
| Slow burn | 30 min      | 3 min        | 6×        | 5%                 | ~10 min      |


We use a **6-hour rolling error budget**, not 30 days. This is a stated demo compression and we
say so out loud in the writeup — a 30-day budget cannot burn 2% inside a 90-second demo, and
pretending otherwise while quietly using a static threshold would be the dishonest version.

Recording rules precompute the SLI ratios at each window so the detector's own PromQL stays
trivial. The detector is **our own Python service polling Prometheus every 15 s** — not
Alertmanager. Detection is Plane 3, it is the thing that makes the system autonomous, and it
should be code we own and can show, not YAML in someone else's binary.

### C.5 How evidence tools map onto the stack

This table is the contract between Plane 2 and Plane 4, and it is the part that must not be
allowed to drift.


| Tool                                    | Backend                              | Concretely                                                                                                                                                                                                                                                              |
| --------------------------------------- | ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `log_stats(service, window, group_by)`  | Loki                                 | LogQL aggregation over the window, then Drain3 templating over the returned lines. Returns: per-template count, delta vs. the same window 24 h prior, and a `novel: true` flag for templates absent from the prior 7 days' template store. **Never returns raw lines.** |
| `log_sample(template_id, window, k≤50)` | Loki                                 | Fetches at most 50 lines matching one template. Hard cap enforced in code, not in the prompt.                                                                                                                                                                           |
| `metric_query(promql, window)`          | Prometheus                           | Executes the query, downsamples to ≤120 points, and attaches the same query shifted 24 h as a baseline series plus a computed delta.                                                                                                                                    |
| `trace_search(filters)`                 | Tempo                                | TraceQL for slow exemplars; returns span-duration breakdown, not full trace JSON. Also supports lookup by a `trace_id` lifted from a log line.                                                                                                                          |
| `git_diff_since(ts, services)`          | Postgres `deployments` + local git   | Rows from `deployments` in the window, joined to `git show` output, truncated per-file.                                                                                                                                                                                 |
| `runtime_state(service)`                | cAdvisor via Prometheus + Docker API | Restart count, OOM kills, current mem/CPU, **current** `config_hash` and when it last changed. The config-hash delta is what solves F-01 and F-03.                                                                                                                      |
| `doc_search(query, tier)`               | Postgres + `pgvector`                | BM25 + `text-embedding-3-small`, reciprocal-rank fusion, over a hand-written corpus: 5 runbooks, 3 architecture docs, 2 ADRs, 3 past postmortems. Small corpus, written by us, tier-labelled.                                                                           |


Every one returns an `Evidence` object per `ARCHITECTURE.md` §6 and passes the redaction gate.

> **Generalisation seam:** these seven tools are the *entire* interface between sherLOG and any
> monitored system. Swapping Loki→CloudWatch or Prometheus→Datadog in v2 means reimplementing
> seven functions behind a fixed signature — not touching the investigation engine.

---



## Part D — The reasoning stack

Parts A–C define the substrate. This part defines the engine: what framework runs it, how data
becomes context, how the verdict is actually computed, which models run which stage, and what
happens when a model is unavailable.

### D.1 Framework decision: no orchestration framework

**We use the OpenAI Python SDK directly, with Pydantic for schemas and a hand-written state
machine. No LangChain, no LangGraph, no CrewAI, no AutoGen.**

The reasoning is short. `ARCHITECTURE.md` §7 commits us to a *staged state machine with a
hypothesis ledger* and explicitly rejects open-ended ReAct. Agent frameworks earn their keep by
supplying exactly the thing we have decided not to build — a dynamic tool-calling loop with
memory and routing. What we actually need is six functions that each take typed input, call one
model with one schema, and return typed output. That is roughly 300 lines of our own code, and
every one of those lines is inspectable at 2 a.m. when a stage misbehaves.

There is a second reason, specific to this project. Our thesis is *verifiability*. Every
intermediate artifact — evidence IDs, predicted signals, per-evidence verdicts, posterior updates
— has to be persisted, queryable, and shown in the UI. Frameworks hold that state in their own
abstractions and hand you a callback surface to get it back out. We would spend the time we saved
on orchestration fighting to extract the ledger, and we would still not own the schema.

| Rejected | Why not, specifically |
|---|---|
| **LangChain** | Its value is the integration surface (loaders, retrievers, 100+ tool wrappers). We have exactly 7 tools, all bespoke, all querying systems we run ourselves. We would import a large dependency and use ~2% of it, while inheriting its abstraction churn. |
| **LangGraph** | The closest call, and a legitimate choice — our design *is* a state graph. Rejected because our graph is linear with two loop-backs; expressing it as `if stage == X` costs less than the concept overhead, and node-level state persistence is something we need in *our* Postgres schema anyway, so the built-in checkpointer buys nothing. **If we later go multi-agent-dynamic, this is the migration target.** |
| **CrewAI / AutoGen** | Built around conversational multi-agent delegation. Our subagents do not converse; they are isolated function calls returning a fixed schema. |
| **DSPy** | Genuinely interesting for prompt optimisation, and the wrong shape for an 8-week build — it wants a training set we do not have until the eval harness exists. Note it as v2 work: once we have 200 scored runs, DSPy over the hypothesize stage is a real experiment. |

**What we do take off the shelf**, because these are solved problems and not our contribution:

| Purpose | Library |
|---|---|
| LLM calls + structured outputs | `openai` SDK, native JSON-schema structured outputs |
| Schemas everywhere | `pydantic` v2 — one model per stage in/out, reused as the OpenAI schema |
| Log templating | `drain3` |
| Retries / backoff | `tenacity` |
| Doc retrieval | BM25 over the runbook corpus (v1; embeddings deferred) |
| Lexical retrieval | Postgres `tsvector` full-text search — **not** Elasticsearch |
| Agent self-tracing | `langsmith` (see D.6) |
| Eval orchestration | plain `pytest` + a runner script |

> **Generalisation seam:** all model access goes through one `LLMClient` protocol with a single
> method, `complete(stage, messages, schema) -> (parsed, usage)`. Nothing else in the codebase
> imports `openai`. Swapping or adding a provider is one class.

### D.2 Model routing and the fallback ladder

Per-stage routing, matching `ARCHITECTURE.md` §7. Roles are named; concrete model IDs live in
`models.yaml` and are pinned, never floating aliases:

| Stage | Role | Why |
|---|---|---|
| Triage | `fast` | Classification against a fixed taxonomy. No reasoning needed. |
| Hypothesize | `reasoning` | The stage that determines the ceiling on everything downstream. Spend here. |
| Gather (per-subagent) | `fast` | Tool selection + summarisation into a fixed schema. |
| Evidence assessment | `fast` | Labelling one evidence item against one hypothesis (see D.4). High call volume, narrow task. |
| Verify | `reasoning` | Reconciling contradictory evidence. |
| Critic | `reasoning`, fresh context | Adversarial falsification is the hardest prompt in the system. |
| Report | `balanced` | Writing to a schema from already-settled conclusions. |
| Eval judge | `reasoning`, different rung from Report | See D.7. |

**Two rules about model IDs, both non-negotiable:**

1. **Pin dated model IDs, never floating aliases.** A provider silently updating the model behind
   an alias mid-project would invalidate every accuracy trend line we have — and we would not be
   able to tell that from a real regression. The model ID is an experimental variable; treat it
   like one. Model changes get a row in a `model_versions` table and a vertical line on the
   accuracy chart.
2. **Verify current model names, context limits, and prices in week 0 and re-verify in week 7.**
   `ARCHITECTURE.md` §11 already flags this; it applies to the cost model in D.8 too.

**The fallback ladder**, in order, implemented in `LLMClient`:

| # | Condition | Action |
|---|---|---|
| 1 | Transient error (429, 5xx, timeout) | `tenacity` exponential backoff + jitter, 3 attempts |
| 2 | Structured-output validation failure | One re-ask with the Pydantic validation error appended to the message; a second failure fails the stage |
| 3 | Primary model persistently unavailable | Downgrade one rung (`reasoning` → `balanced` → `fast`), **and tag the investigation `degraded=true`** |
| 4 | All rungs unavailable | Terminate the investigation as `INSUFFICIENT_EVIDENCE(reason=provider_unavailable)` |

**The tag is the important part.** Any run with `degraded=true` is excluded from headline accuracy
and reported as a separate line. A run that silently fell back to a weaker model is a different
experiment, and averaging it into the results is how honest projects produce dishonest numbers.

**Cross-provider fallback is deliberately out of v1.** A second provider means a second SDK, a
second structured-output dialect, a second prompt-compatibility surface to maintain, and — worst —
an accuracy number that silently mixes two model families. The `LLMClient` protocol leaves the
seam open; we do not walk through it in eight weeks.

**A local model is ruled out on arithmetic, not preference.** The free-tier box is 4 ARM cores with
no GPU, already running ~9 GB of observability stack. A 7B model there produces a few tokens per
second and is unreliable at strict structured output — it would be slower than the fault it is
diagnosing and would fail the schema gate constantly. Nothing about the design changes if a GPU
appears later; it is a `models.yaml` edit.

### D.3 Data processing: telemetry → context

Four transforms, all deterministic, all before any token is spent. This is where the log-volume
risk from `ARCHITECTURE.md` §14 is actually retired.

**1. Log templating (continuous, not on-demand).** A background job pulls the last minute of logs
from Loki every minute and runs Drain3, maintaining `log_templates(template_id, pattern,
first_seen, last_seen, hourly_counts)` on a 7-day rolling window. Because this runs continuously,
`log_stats` at investigation time is a *table read*, not a batch job — and `novel: true` means
"absent from 7 days of history", which is a claim we can actually stand behind. Doing templating
on-demand inside an investigation would add tens of seconds to MTTD and make novelty detection
depend on how much history we happened to fetch.

**2. Metric downsampling.** Every `metric_query` returns at most 120 points (step computed from
the window), plus the same query shifted 24 h as a baseline series, plus a computed delta. The
model never sees a raw series; it sees shape and change.

**3. The Evidence store, content-addressed.**

```
evidence(
  id            text primary key,   -- 'ev_' || left(sha256(kind|query|t_start|t_end), 8)
  investigation_id, kind, query, t_start, t_end,
  content_redacted text,
  raw_hash      text,               -- sha256 of PRE-redaction content, for audit
  source_ref    text,
  retrieved_at  timestamptz
)
```

The ID being a hash of `(kind, query, time_range)` is not a detail. It means two subagents issuing
the same query get the *same* evidence ID — so citations are stable, the cache is automatic, and
we never pay twice for the same retrieval. It also makes the citation verifier's existence check
trivially sound.

**4. The redaction gate as a type boundary.** `Evidence` is constructible by tools;
`RedactedEvidence` is constructible *only* by the gate. `LLMClient.complete()` accepts
`RedactedEvidence` and nothing else. The compiler enforces §9's chokepoint, so no future
contributor can accidentally route around it.

**Context assembly** is a single function with a per-stage token budget:

| Stage | Budget |
|---|---|
| Triage | 6 k |
| Hypothesize | 12 k |
| Gather subagent (each) | 15 k |
| Evidence assessment (each) | 2 k |
| Verify | 20 k |
| Critic | 10 k |
| Report | 12 k |

Evidence is rendered inside fixed delimiters with the untrusted-data preamble from §9.5. If
selected evidence exceeds the budget, it is truncated by *relevance rank*, and the truncation is
recorded in the run ledger — a stage that ran on truncated context is a fact the eval needs to know.

### D.4 How the final decision is actually made

This is the part that most distinguishes the system from a wrapper, so it is specified exactly.

**The model labels; the code computes.** LLMs are good at judging whether a piece of evidence
supports a claim, and bad at arithmetic over many such judgments — asking one for a final
confidence number produces a fluent guess. So the model never emits a posterior. It emits a
discrete label per (hypothesis, evidence) pair:

```python
class Assessment(BaseModel):
    hypothesis_id: str
    evidence_id: str
    verdict: Literal["STRONG_SUPPORT","WEAK_SUPPORT","NEUTRAL",
                     "WEAK_REFUTE","STRONG_REFUTE","DECISIVE_REFUTE"]
    rationale: str          # one sentence, shown in the UI
```

Code maps labels to fixed log-likelihood-ratio weights and updates in log-odds:

| Verdict | Weight (nats) |
|---|---|
| `STRONG_SUPPORT` | +1.4 |
| `WEAK_SUPPORT` | +0.5 |
| `NEUTRAL` | 0.0 |
| `WEAK_REFUTE` | −0.5 |
| `STRONG_REFUTE` | −1.4 |
| `DECISIVE_REFUTE` | −4.0 |

```
posterior(H) = sigmoid( logit(1/N) + Σ w_i )      # N = number of hypotheses
then normalised across all open hypotheses to sum to 1
```

**Three guards on the arithmetic, each fixing a real failure mode:**

- **Deduplication.** Each `evidence_id` contributes to a given hypothesis at most once. Content
  addressing (D.3) makes this exact.
- **Correlated-evidence cap.** Total weight from a single `kind` (log/metric/trace/git/state/doc)
  toward one hypothesis is clamped to ±2.0. Ten log lines saying the same thing are one fact, and
  without this clamp the log tool alone can drive any hypothesis to certainty.
- **Mechanical falsification.** A missing `predicted_signal` is *not* an LLM judgment. If a
  hypothesis pre-registered a signal that the evidence layer can check, and the check returns
  absent, code applies −2.0 with no model in the loop. This is the ledger's whole point from
  `ARCHITECTURE.md` §7 made operational: the agent cannot narrate its way past a failed prediction.

**The outcome gate is pure code, no LLM:**

```python
if budget_exhausted:                     return INSUFFICIENT_EVIDENCE(reason="budget")
if unverified_claims > 0:                mark PARTIALLY_VERIFIED, cap confidence at 0.70
if top.posterior >= 0.80 and margin >= 0.25 and critic == "CONCUR":
                                         return CONFIRMED
if top.posterior >= 0.55:                return LIKELY          # 0.79 cap if critic dissents
                                         return INSUFFICIENT_EVIDENCE(reason="low_confidence")
```

`margin = top.posterior − second.posterior`. **The margin requirement is what makes the F-01/F-02
confusable pair scientifically meaningful.** If cache-TTL and Redis-eviction both sit near 0.5
because the agent never fetched the discriminating signal, margin fails and the system abstains
instead of coin-flipping. Abstention there is the *correct* behaviour, and it is only reachable
because confidence is computed rather than asserted.

**Budget governor** (hard caps, enforced in code, checked before every model call):
250 k total tokens · 60 tool calls (v1; was 40, below the 43-call worst case) · 8 min wall clock · $0.60 per investigation. Breaching any one
exits through the `budget` branch above with partial findings attached — never a guess.

### D.5 Determinism, caching, and replay

- `temperature=0` and a fixed `seed` on every call. This reduces variance; it does not eliminate
  it, so per `ARCHITECTURE.md` §14 every fault is run 3× and reported as mean ± spread.
- **Evidence cache** keyed by the content-addressed ID, TTL 1 h.
- **LLM response cache** keyed by `sha256(model_id, messages, schema)`, stored in Postgres. This
  is what makes CI affordable — see D.8.
- **Three run modes**, one flag:

| Mode | Evidence | LLM | Used for |
|---|---|---|---|
| `live` | real backends | real calls | demo, weekly eval |
| `replay` | recorded fixtures | real calls | nightly eval — no need for a running target system |
| `frozen` | recorded fixtures | cached responses | every commit in CI, **$0**, tests the deterministic layers |

`frozen` mode is what lets us regression-test the citation verifier, the posterior arithmetic, the
redaction gate, the detectors, and the outcome gate on every push without spending a cent. Those
components are most of the thesis, and they are all deterministic — they deserve real test coverage.

### D.6 Observing the agent itself: LangSmith

> **Revised 2026-11-26 (v1 build).** The original plan made LangSmith optional and sent the
> agent's own spans to a second Tempo. v1 does the reverse, for the reasons below.

**LangSmith is the agent tracer.** Every investigation is one nested LangSmith trace:
`investigation` -> `stage:triage | hypothesize | gather | verify | critic | report` -> each
model call (via `langsmith.wrappers.wrap_openai`) and each evidence-tool call (`@traceable`).
It is enabled whenever `LANGSMITH_API_KEY` is set (project `sherlog`).

Why this is safe and why it replaces the second Tempo:

- **It cannot leak into evidence.** LangSmith is outside the monitored system. The original
  hazard, the agent retrieving its own reasoning as "evidence" from the Tempo it queries,
  disappears when the agent's traces never enter that Tempo at all. `tempo-sherlog` is
  removed.
- **It sees nothing the model does not.** Only `RedactedEvidence` can reach a prompt (a
  type boundary in `LLMClient.complete`), so the tracer receives exactly the redacted
  payload the model receives.
- **It is not where the numbers come from.** Decoy resistance, strip-rate, abstention
  correctness and accuracy against ground truth are computed by `eval/score.py` from the run
  ledger. LangSmith is for seeing *why* a stage did what it did.

**The run ledger is still ours.** Each investigation is persisted as one JSON document
(`runtime/investigations/<id>.json`): hypotheses with their pre-registered predictions and
the code's check results, every assessment, every evidence item (query, window, redacted
content), the critic, the report, the citation-verification result, per-stage tokens and
cost, and the outcome. The UI renders it and the eval scores it.

### D.7 The eval judge — and how to not fool ourselves with it

An LLM judge is the standard way projects like this quietly inflate their own numbers. Two
mitigations:

**Most metrics need no judge at all.** `proximate_service` and `originating_component` are exact
string matches against the fault YAML. Decoy resistance is a set-membership check. Evidence
precision and hallucination rate come from the programmatic citation verifier. Abstention
correctness is a comparison of two enums. **Only root-cause-text equivalence and causal-chain F1
need a model** — so judge exposure is confined to two of nine metrics.

**Where a judge is used:** it runs the `reasoning` role at a *different rung than the Report stage
used*, receives the ground-truth YAML and a written rubric, and outputs a structured verdict with
justification. Then: **20% of judged runs are hand-scored, and judge-human agreement is reported
as a headline number.** If agreement is under ~90%, the judge is not fit and the rubric gets
rewritten. Publishing that agreement rate is the difference between an eval and a vibe.

The rubric gets written in week 0, before any results exist — `ARCHITECTURE.md` §15 item 5 is
right that grading generously is unconscious, and the only defence is writing the definition down
while you still have nothing to lose by it.

### D.8 Cost model, with the arithmetic shown

Per-investigation estimate, to be recomputed at week 0 against live prices:

```
triage            ~5 k in / 0.5 k out   fast
hypothesize      ~12 k in / 2 k out     reasoning
gather × 4       ~60 k in / 4 k out     fast
assessments × 20 ~40 k in / 2 k out     fast
verify           ~20 k in / 2 k out     reasoning
critic           ~10 k in / 1 k out     reasoning
report           ~12 k in / 3 k out     balanced
                 ─────────────────────
                 ~160 k in / 14 k out, roughly 80/20 fast-to-reasoning
```

At mixed-tier pricing this lands in the **$0.15–0.35** range per investigation, which is why the
governor caps at $0.60 — a run that doubles the estimate is a bug worth failing on, not absorbing.

**The eval budget only works with tiering.** The full matrix is 39 runs; at $0.25 that is ~$10 per
full pass. Nightly full passes for eight weeks would be ~$550 against a $40–70 budget. So:

| Tier | Cadence | Scope | Cost |
|---|---|---|---|
| **Commit** | every push | `frozen` mode, deterministic layers only | **$0** |
| **Nightly** | daily | 6-fault smoke, 1 rep each, `replay` | ~$1.50/night |
| **Full** | weekly, weeks 6–8 only | 39-run matrix, `live` | ~$10/pass |

Total: roughly **$1.50 × 40 + $10 × 3 ≈ $90** of eval, plus development-time calls. That is
above the §11 estimate, so one of two levers gets pulled: drop nightly smoke to 3 faults, or run
the full matrix twice instead of three times. **Decide this in week 0 and put a hard spend limit
on the API key** — discovering the overrun in week 7 means losing the accuracy trend line, which
is the single most valuable artifact the project produces.

---



## Part E — What this buys us, and what it costs

**The v1 eval matrix:** 6 faults × 3 repetitions × 2 decoy conditions = **36 scored runs** per
nightly cycle, plus 3 runs of the abstention case. Enough for a trend line with visible error
bars; small enough to run nightly on GitHub Actions free minutes against cached evidence fixtures.

**The four claims v1 can defend with numbers:**

1. RCA accuracy overall, and specifically on the F-01/F-02 confusable pair (where a
  pattern-matching agent lands at ~50%).
2. Decoy resistance — the D-2 benign-deploy trap gives a clean number.
3. Hallucination rate via citation-verification strip-rate.
4. Correct abstention on F-07 and correct non-abstention on the other six.

**What we knowingly give up:** fault-class breadth. v1 has no memory leak, no disk fill, no clock
skew, no schema drift, no prompt-injection fault. Each is a YAML entry and an injector method once
the harness exists — that is the week-7 backlog, in this order: memory leak (git commit, worker),
prompt-injection-in-logs (data), disk fill (chaos), schema drift (git commit).

---



## Open items to close in week 0

- [ ] Verify `arm64` images for Toxiproxy and `postgres_exporter`; build from source if absent.
- [ ] Provision the Oracle Cloud Always Free ARM box and SSH in. Do not assume capacity is available — free-tier ARM is frequently out of stock in a given region, and Hetzner CX22 at ~€4/mo is the fallback.
- [ ] Write the 6 fault YAMLs in full, including `known_misdiagnosis` for all of them.
- [ ] Author the 13 corpus documents (they must be genuinely useful, not filler — the doc-RAG ablation is meaningless if the docs contain no information).
- [ ] Confirm Drain3 template stability against a 1-hour sample of real generated logs before relying on `novel: true`.
- [ ] Verify current model IDs, context limits, and per-token prices; pin dated IDs in `models.yaml` and recompute the D.8 cost table against them.
- [ ] Set a hard monthly spend limit on the API key, and decide the eval-tier lever (3-fault nightly vs. two full passes) before week 1.
- [ ] Write the eval judge rubric — before any results exist.
- [ ] Stand up the second Tempo instance for sherLOG's own telemetry; write the isolation test alongside the ground-truth one.