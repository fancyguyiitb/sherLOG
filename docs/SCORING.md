# Scoring definitions

**Written in week 0, before any results exist.**

That timing is the whole point. Grading generously is unconscious, not dishonest — you look
at an answer that is *nearly* right, and "nearly" quietly becomes "correct" because you know
what the system was reaching for. The only defence is writing the definition down while you
still have nothing to lose by making it strict.

Nothing in this document may be revised after the first scored run without recording the
revision, the date, and the reason, and re-scoring every prior run under the new definition.

---

## 1. What counts as an answer

Every investigation terminates in exactly one outcome:

| Outcome | Reached when |
|---|---|
| `CONFIRMED` | posterior ≥ 0.80, margin ≥ 0.25, critic concurs, all claims verified |
| `LIKELY` | posterior ≥ 0.55 (capped at 0.79 if the critic dissents) |
| `PARTIALLY_VERIFIED` | any claim failed citation verification; confidence capped at 0.70 |
| `INSUFFICIENT_EVIDENCE` | posterior < 0.55, or budget exhausted, or the provider was unavailable |

The outcome is computed by code from the ledger. No model is asked what the outcome
should be.

---

## 2. The nine metrics

Seven of the nine need no model at all. Confining judge exposure to two metrics is
deliberate: an LLM judge is the standard way projects like this quietly inflate their own
numbers.

| # | Metric | Definition | Judged by |
|---|---|---|---|
| 1 | **RCA accuracy@1** | Does the top hypothesis state the ground-truth root cause? | model + rubric |
| 2 | **Service localization** | Exact string match on `proximate_service` | code |
| 3 | **Origin localization** | Exact string match on `originating_component` | code |
| 4 | **Causal chain F1** | Overlap of the stated chain against the ground-truth chain | model + rubric |
| 5 | **Evidence precision** | Share of cited claims passing programmatic verification | code |
| 6 | **Hallucination rate** | Claims failing verification ÷ total claims generated | code |
| 7 | **Decoy resistance** | Share of runs in which no decoy was named as a cause | code |
| 8 | **Abstention correctness** | Abstains on F-07; does not abstain on the other six | code |
| 9 | **MTTD / MTTR-diagnosis** | Injection→detection, and detection→verdict, in seconds | code |

Cost per investigation (tokens and dollars) is recorded on every run and reported alongside,
but it is a budget figure rather than a quality metric.

### Metrics 2 and 3 are scored separately, and this matters

F-04's proximate service is `payments-svc` and its originating component is `psp-stub`.
An answer naming `payments-svc` as the root cause scores **1 on metric 2 and 0 on metric 3**.
Collapsing the two into "did it find the right service" would hide the exact distinction
the fault was built to test.

---

## 3. When the judge is used, and how it is kept honest

The judge runs the `reasoning` role at a **different rung than the Report stage used**,
receives the ground-truth YAML and a written rubric, and returns a structured verdict with
justification.

Two constraints on it:

1. **20% of judged runs are hand-scored**, and judge–human agreement is reported as a
   headline number alongside the accuracy figures.
2. **If agreement falls below ~90%, the judge is not fit for purpose** and the rubric is
   rewritten before any results derived from it are reported.

Publishing that agreement rate is the difference between an evaluation and a vibe.

---

## 4. Partial credit, stated in advance

| Situation | Score |
|---|---|
| Names the correct root cause and mechanism | full |
| Names the correct proximate service but the wrong originating component (the F-04 trap) | metric 2 only |
| Names the trigger but not the defect (F-06: "there is a bad row", missing the deleted dead-letter branch) | half on metric 1 |
| Names a decoy as the cause | 0 on metric 1, **and** fails metric 7 |
| Correct answer reached with a claim that failed verification | scored, but the run is `PARTIALLY_VERIFIED` and reported separately |
| Abstains on a solvable fault | 0 on metric 1, fails metric 8 |
| Abstains on F-07 with a stated partial finding | **full marks** |

---

## 5. Runs excluded from headline numbers

Excluded, and reported as separate lines rather than silently dropped:

- **`degraded=true`** — the run fell back to a weaker model rung because the primary was
  unavailable. A run on a different model is a different experiment, and averaging it in is
  how honest projects produce dishonest numbers.
- **`provider_unavailable`** — no model was reachable; the run has no diagnostic content.
- **Runs whose evidence layer errored** — a tool failure is an infrastructure result, not a
  reasoning result.

Runs that hit the budget governor are **not** excluded. Exceeding budget is a real outcome
of a real design decision and belongs in the numbers.

---

## 6. The eval matrix

6 faults × 3 repetitions × 2 decoy conditions = **36 scored runs**, plus 3 runs of the
abstention case.

Three repetitions because `temperature=0` and a fixed seed reduce variance without
eliminating it. Every result is reported as **mean ± spread**, never as a single number.

| Tier | Cadence | Scope | Cost |
|---|---|---|---|
| Commit | every push | `frozen` mode, deterministic layers only | **$0** |
| Nightly | daily | 6-fault smoke, 1 rep each, `replay` mode | ~$1.50 |
| Full | weekly, weeks 6–8 | the full 39-run matrix, `live` | ~$10 |

---

## 7. The four claims v1 is designed to defend

1. **RCA accuracy overall**, and specifically on the F-01/F-02 confusable pair, where a
   pattern-matching agent lands near 50%.
2. **Decoy resistance** — the D-2 benign-deploy trap produces a clean number.
3. **Hallucination rate**, via the citation-verification strip-rate.
4. **Correct abstention** on F-07, and correct *non*-abstention on the other six.

An ablation exists for each mechanism the design claims credit for — no-critic, no-docs,
no-git, no-ledger — so that "the ledger helped" is a measured delta rather than an assertion.

---

## Revision log

### 2026-11-26 — v1 build: judge deferred, metric 1 scored by code

**Recorded before the first scored run.** The v1 build ships without the LLM judge, so:

- **Metric 1 (RCA accuracy@1) is scored by code.** A run scores 1.0 when it did not abstain,
  its leading hypothesis names the ground-truth *originating component*, its statement
  names the fault's *mechanism* (a per-fault pattern), and its declared fault class is in
  the accepted set for the fault (table in `eval/score.py`, `ACCEPT`). The mechanism
  requirement was added during development, before any matrix run, after a cache-TTL
  diagnosis of the pool fault F-03 scored as correct: F-01 and F-03 share both component
  and class. F-06 scores 0.5 when the class is `data` (the bad row — the
  trigger) rather than `code_regression` (the deleted dead-letter branch — the defect),
  per §4. F-07 scores 1.0 when the run abstains, names `payments-svc` as the proximate
  service, and states an evidence gap. Naming a decoy as the cause scores 0.
- **Decoy blame is detected by code:** D-1 when the leader names Redis with a
  latency/network mechanism (not eviction/memory); D-2 when the leader or root cause cites
  the decoy commit's SHA or describes a log-wording/README change.
- **Metric 4 (causal-chain F1) is deferred** with the judge; it is not reported in v1.
- **Abstention correctness** counts `INSUFFICIENT_EVIDENCE` only. `PARTIALLY_VERIFIED` is a
  (downgraded) answer, not an abstention.

Why: a code-scored metric is stricter and fully reproducible; it can only under-credit a
correct answer phrased with an unexpected fault class, never over-credit a wrong one.

### 2026-11-26 — proximate service, definition made explicit

Before the first matrix run. The report schema described `proximate_service` as "where
users feel it first", which the model read as the gateway on every fault; the catalog has
always meant the first service on the causal path whose own behaviour degrades (orders-svc
for F-05, payments-svc for F-04). The schema's instruction now states the catalog's
definition. Ground truth is unchanged.
