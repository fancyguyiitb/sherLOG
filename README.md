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

## Running it

```bash
make up                  # target system + observability + continuous load
make test                # the deterministic suite — no LLM calls, no cost
make catalog             # list the fault catalog
make inject F=F-04       # inject a fault and its declared decoys
make surprise            # inject a random fault without printing which one
```

The whole stack runs in Docker Compose on one free-tier ARM box. **Cost: $0** for
everything except model calls, which are capped at $0.60 per investigation by a governor
enforced in code.

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
