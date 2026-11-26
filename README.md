# sherLOG

**A verifiable AI SRE.** An agentic system that diagnoses production incidents autonomously,
and whose correctness is continuously measured against injected ground truth.

The thesis is not "an AI SRE". It is the word *verifiable*.

---

## Why this can exist

Commercial AI-SRE products cannot tell you how accurate they are. In a customer's
production environment nobody knows the true root cause of an incident, so there is nothing
to score against. Accuracy claims are testimonial.

sherLOG owns its environment and injects its own faults. So it knows the true root cause of
every incident it investigates, and "RCA accuracy is 84%" becomes a measurement with a
method behind it.

That single fact converts the vaguest requirement in this space — *guardrails against
hallucination* — from hand-waving into a graphed, defensible system property.

---

## What is in the repository

```
nimbus/         Plane 1 — the target system: five services, two datastores, one chaos proxy
  nimbus_common/    logging, metrics, config-hash, instrumented HTTP — one shared package
  gateway/          public edge, owns timeouts and retries, no DB
  orders_svc/       Postgres pool + Redis cache; three faults originate here
  payments_svc/     calls the payment provider through Toxiproxy
  worker/           job loop; the dead-letter branch F-06 deletes
  psp_stub/         fake external payment provider
  db/               schema, the deployments table, and the role separation

ops/            Plane 2 — Loki, Prometheus (+ SLO recording rules), Tempo, Alloy, Toxiproxy
load/           k6 profile: 13 rps, diurnal, cohort-split, with a deliberate error floor

sherlog/
  detect/       Plane 3 — multi-window burn-rate detection and alert correlation
  evidence/     Plane 4 — the seven-tool contract, metric shaping, log templating
  redaction.py  the gate between Plane 4 and Plane 5
  engine/       Plane 5 — hypothesis ledger, posterior arithmetic, budget, outcome gate
  grounding/    Plane 6 — programmatic citation verification
  llm/          the single seam through which every model call passes

faults/         the fault catalog: 6 faults + 2 decoys + 1 abstention case, with ground truth
tests/          the deterministic layers, tested on every push for $0
```

---

## The six mechanisms that make it more than a wrapper

**1. A fault catalog with ground truth.** Nine declarative YAML files, each carrying both
the recipe for injecting a fault and the truth about what it does. Every mechanism is real:
config changes force a container restart, code regressions ship as genuine git commits with
plausible messages, dependency faults are L4 toxics on real network hops. Nothing is
`if FAULT_FLAG: sleep()`.

**2. A hypothesis ledger with pre-registered predictions.** Hypotheses declare what *must*
be true if they hold, before any evidence is gathered for them. A prediction that later
checks as absent refutes its hypothesis mechanically, with no model in the loop. This is
what converts the model from a narrator — which will always find a story — into a falsifier.

**3. The model labels; the code computes.** No model in this system ever emits a
confidence number. Models emit discrete verdicts per (hypothesis, evidence) pair; code maps
those to fixed log-likelihood ratios and does the arithmetic, with three guards: evidence
deduplication by content address, a per-signal-kind cap so ten log lines cannot become ten
facts, and uncapped mechanical falsification.

**4. Programmatic citation verification.** Every claim carries evidence IDs. Four
deterministic checks — does the evidence exist, does its window contain the claimed
timestamp, do the numbers appear within tolerance, are the named entities present — strip
claims that fail. The strip-rate is a reported metric.

**5. Calibrated abstention.** `INSUFFICIENT_EVIDENCE` is a success state. Two faults are
designed to reach it: F-07, where the evidence genuinely is not there, and the F-01/F-02
pair, where an agent that gathered only the confirming signal ends up with two near-tied
hypotheses and no margin. Abstention there is the correct answer, and it is only reachable
because confidence is computed rather than asserted.

**6. Security enforced by types and grants, not discipline.** `RedactedEvidence` is
constructible only by the redaction gate, and the LLM client accepts nothing else. Ground
truth lives in a separate database whose role the engine holds no grant on, asserted by a CI
test. A canary secret is planted in a log line, a config value and a corpus document, and CI
asserts it never reaches an outbound payload.

---

## The two confusable pairs

The scientific content of the evaluation lives here. Each pair produces a nearly identical
symptom from a different cause, so the discriminating signal has to be sought deliberately.

| Pair | Same symptom | Separated only by |
|---|---|---|
| **F-01** cache TTL collapse vs **F-02** Redis eviction storm | cache misses up, Postgres load up, gateway p99 breaches, zero errors | `redis_evicted_keys_total` climbing vs flat, and *which service's* config hash moved |
| **F-03** DB pool exhaustion vs **F-04** payment provider latency | pool waiters elevated, gateway 504s | `upstream_duration_seconds{upstream="psp"}`, and whether pool config actually changed |

F-04 is the trap: it elevates pool waiters too, because blocked handlers hold connections.
An agent that stops at "the pool is exhausted" gets the proximate service right and the root
cause wrong — and the catalog records that specific wrong answer in advance, as
`known_misdiagnosis`, so "wrong" becomes "wrong in the way we predicted".

---

## Results (v1 matrix, measured)

39 autonomous runs on engine commit `71fd514`: 6 faults x 3 repetitions x 2 decoy
conditions, plus 3 runs of the abstention case F-07. Every run was injected, detected,
investigated and scored with no human in the loop. Scoring is code-only and was defined
before the run. Full tables: [`docs/RESULTS.md`](docs/RESULTS.md).

| Metric | Result |
|---|---|
| Detection (SLO burn-rate alert within 8 min of injection) | 97% (38/39) |
| RCA accuracy@1, end to end (undetected runs count as misses) | 0.75 ± 0.43 |
| Correct abstention on the unobservable fault F-07 | 3/3 |
| Decoy resistance (benign deploy / mild Redis latency never blamed) | 18/18 |
| Origin localisation | 87% |
| Claims stripped by citation verification | 12.0% (20/167) |
| MTTD (injection -> alert) | 77 ± 46 s |
| Detection -> verdict (includes a fixed 60 s settle) | 107 ± 6 s |
| Model cost per investigation | $0.09 ± $0.02 (matrix total $3.67) |

Per fault, RCA@1: F-01 cache TTL 6/6 · F-05 N+1 regression 6/6 · F-04 slow provider 5/6 ·
F-03 pool size 4/6 (one run injected at the traffic trough was never detected) ·
F-06 dead-letter removal 3/6 · F-02 Redis eviction 3/6.

**What the misses are.** Almost every miss is the system *declining* to answer, not answering
wrongly. Across the 36 solvable runs it gave a scored-wrong answer in 2, both on F-06,
where it named the right component and deploy but described the mechanism too vaguely for
the strict scorer ("broke job completion"). The rest are abstentions:

- F-02: two well-supported rivals tie under the per-evidence-kind cap and the margin gate
  declines.
- F-03 and F-04: one abstention each on a low margin.
- F-06: one engine failure (fewer than three distinct hypotheses), scored as a failure, not
  retried away.

Each is written up with its evidence in [`docs/BUILD_LOG.md`](docs/BUILD_LOG.md) §4.

Service localisation (63%) is the weakest metric. It stays at 0% on F-02 and F-04, where
the reports name the gateway or orders-svc instead of the catalog's proximate service.

---

## Running it

Prerequisites: Docker Desktop (8 GB), Python 3.12+, and a `.env` at the repo root with
`OPENAI_API_KEY` and `LANGSMITH_API_KEY`.

```bash
make venv                # .venv with the engine, injector, eval and UI
make up                  # builds the deploy repo, starts target system + observability + load
make test                # the deterministic suite: no LLM calls, no cost
make ui                  # glass-box UI on http://localhost:8080 (fault console, live ledger, eval)
make detect              # the autonomy loop: burn-rate detector -> incident -> investigation
make inject F=F-04       # or use the UI's fault console
make reset               # revert every fault to the clean baseline
make eval                # the 39-run matrix (6 faults x 3 reps x 2 decoy modes + 3x F-07)
```

Model calls go to `gpt-4.1-mini-2025-04-14` (triage, gather, assess) and
`gpt-4.1-2025-04-14` (hypothesize, verify, critic, report), pinned to dated snapshots.
Every investigation is traced to LangSmith (project `sherlog`) as one nested trace:
stage -> model call / tool call.

---

## How an investigation actually runs

1. **Detect.** `sherlog/detect/service.py` polls the SLO recording rules every 15s and
   evaluates multi-window burn rates (fast: 5m and 30s windows above 14.4x; slow: 30m and
   3m above 6x). Alerts are correlated into one incident. No human starts anything.
2. **Triage** (`fast`). Seven fixed snapshot queries, the same every time, so triage cannot
   be steered by what it chose to fetch. The model only classifies.
3. **Hypothesize** (`reasoning`). 3-5 competing hypotheses, each registering 2-4
   **checkable predictions** (tool + arguments + condition from a fixed vocabulary) *before*
   any evidence for it is gathered. Uncheckable predictions are rejected by code.
4. **Gather.** Per hypothesis, in isolation: code runs each prediction's query and
   evaluates its condition: **present**, **absent** (costs the hypothesis -2.0 nats, applied
   mechanically), or **no data** (an evidence gap, deliberately *not* an absence). A
   `fast` subagent that sees only that hypothesis may then request up to three more queries.
5. **Verify** (`fast`). Every (hypothesis, evidence) pair gets a discrete label, judged
   against the competing hypotheses so shared symptoms count as neutral. Code maps labels to
   fixed log-likelihood ratios, applies dedup and the per-evidence-kind cap, and computes
   posteriors. No model ever emits a probability.
6. **Critic** (`reasoning`, fresh context). Argues the leading conclusion is wrong. A
   dissent caps confidence.
7. **Report** (`balanced`). Causal-chain claims, each citing evidence IDs. Code extracts
   every number, service and commit SHA from each claim and checks that they appear in the
   cited evidence, inside its window. Failing claims are stripped and counted.
8. **Outcome gate** (code). `CONFIRMED` / `LIKELY` / `PARTIALLY_VERIFIED` /
   `INSUFFICIENT_EVIDENCE` from posterior, margin, critic and verification. Abstention is a
   success state.

---

## v1 build: where it departs from the plan

Recorded so the docs never claim more than the code does.

| Plan | v1 | Why |
|---|---|---|
| Agent spans to a second Tempo; LangSmith optional | LangSmith is the agent tracer; `tempo-sherlog` removed | See V1_SCOPE §D.6 |
| Hybrid BM25 + pgvector doc retrieval | BM25 over `docs/runbooks/` | Embeddings add cost and moving parts without changing the mechanism |
| Continuous Drain3 job with 7-day novelty | Drain3 at query time; novelty = absent from the equal-length baseline window just before onset | The stack is hours old, not weeks; a 24h-prior baseline does not exist yet |
| LLM judge for RCA accuracy and causal-chain F1 | RCA accuracy scored by code (`eval/score.py`); chain F1 deferred | Dated revision in docs/SCORING.md |
| `frozen` / `replay` modes | `live` only | Deferred; the deterministic layers are unit-tested instead |
| Fault commits on the project repo | A separate deploy repo (`runtime/deploy-repo`) of comment-stripped code | Source comments name the faults; a diff hunk touching them would leak the answer |
| F-07: stop log shipping only | Full telemetry blackout of payments-svc (logs, traces, `/metrics`) + latency toxic | With only logs stopped, payments-svc's own metrics still name the cause, so abstaining would be wrong |
| F-03: pool 20 -> 3 | pool 20 -> 1 | At ~13 rps a pool of 3 never saturates (measured) |
| Latency SLO: under 1200 ms | under 400 ms (2x the healthy p99 of ~200 ms) | At 1.2s the cache and N+1 faults breach nothing |

---

## The demo that has to work

A reviewer clicks **Inject: connection pool exhaustion**, then does nothing else. Within
about 90 seconds:

1. An SLO burn-rate detector fires and correlates its alerts into one incident.
2. An investigation opens autonomously. No human input from here on.
3. Three to five hypotheses are posted, each with pre-registered falsifiable predictions.
4. Isolated subagents gather evidence against each.
5. Hypotheses grey out as they are refuted, with the counter-evidence cited.
6. A root cause is stated with a commit SHA, a log signature and a doc reference — and every
   claim expands to show the exact query, time range and redacted payload behind it.
7. The eval tab shows the same pipeline scored across every prior run.

Everything after step 1 runs with zero human input. The frontend observes; it does not
drive.

---

## Documents

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the seven planes and the design decisions behind them
- [`V1_SCOPE.md`](V1_SCOPE.md) — the locked v1 scope: target app, fault catalog, monitoring, reasoning stack
- [`docs/SCORING.md`](docs/SCORING.md) — how a diagnosis is judged correct, written before any results existed
- [`docs/RESULTS.md`](docs/RESULTS.md) — the v1 matrix, every run
- [`docs/BUILD_LOG.md`](docs/BUILD_LOG.md) — every bug found and fixed, open issues, how to resume
