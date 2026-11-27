# Build log — v1 live engine (2026-11-26)

What was built, every bug found, how each was fixed, and what is still open. Written so the
work can be resumed — or re-run — without rediscovering any of it. Commits are on branch
`v1-live-engine`.

---

## 1. Where things stand

| Item | State |
|---|---|
| Testbed (Nimbus, 5 services + Postgres/Redis/Toxiproxy + k6) | running, real SQL / cache / OTel traces |
| Fault injector (config, git commit, chaos, data) + ground truth | working end to end |
| Engine: 7 tools, 6 stages, LangSmith tracing | working |
| Eval matrix 2: 39 runs (6 faults x 3 reps x 2 decoy modes + 3x F-07) | **complete** on commit `71fd514`, 15:22 -> 23:30 UTC 2026-11-26, $3.67 in model calls |
| O1 fix (`1f7ce70`) + live rerun of F-02 / F-03 | done: RCA@1 F-02 3/6 -> 6/6, F-03 4/6 -> 6/6 (section 7) |
| Eval matrix 3: the same 39 runs on the fixed engine | **complete** on commit `0a07fae` (code = `1f7ce70`), 09:44 -> 19:39 UTC 2026-11-27, $3.80 (section 8) |
| Results report | `docs/RESULTS.md`: matrix 3 headline, matrix 2 kept below it. Detection 100%, RCA@1 end-to-end 0.86 +/- 0.35 (31/36, was 0.75), 1 wrong answer (was 2), F-07 abstention 3/3, decoys 18/18, 8.6% claims stripped |

**To resume a matrix if it dies:** `make up` (if the stack is down), then
`.venv/Scripts/python -u -W ignore -m eval.run --out runtime/eval/<name>.jsonl >> runtime/eval/<name>.log 2>&1`
(no `--out` means `runtime/eval/results.jsonl`, matrix 2's file; matrix 3 is `matrix3.jsonl`).
It skips every key already in the output file. Do **not** kill it mid-reset
(only while the log's last line is "waiting for a clean baseline"). Keep the UI server off
while it runs (see bug B12).

**Aborted first matrix:** 8 runs on `fc41fc2`, archived in `runtime/eval/aborted-fc41fc2/`,
superseded by the clean restart on `71fd514` (bugs B16, B17). Not counted.

---

## 2. Commits

| Commit | What |
|---|---|
| `c45fdf0` | Live engine: real testbed, seven tools, six-stage investigation, eval harness |
| `a42be8c` | `negate` predictions; 60s settle after detection |
| `f0bbb82` | Results aggregator (`eval/report.py`) |
| `cca8351` | Joint assessment, change-aware triage, critic held to its own predictions |
| `fc41fc2` | Overnight hardening; evidence-gap gate; duplicate merge; F-02 4mb; TTL jitter |
| `4b4c8ea` | Save retries; non-budget errors never scored |
| `a1ea450` | Coverage gap = silence, not "quieter" |
| `71fd514` | Duplicate merge ignores fault-class label — **the matrix commit** |
| `7e0b8bf` | This build log |
| `61d8703` | Matrix results published (docs/RESULTS.md, README) |
| `1f7ce70` | **O1 fix**: softmax posterior; dark-dependency gate; `eval/rescore.py`; `-dirty` commit stamp |
| `0a07fae` | Build log: O1 fix and rerun (docs only) — **the matrix 3 commit** |

---

## 3. Bugs found, and fixes

Grouped by where they lived. "Found by" says how — most were found by running the system,
not by reading it.

### Testbed (the scaffold had never run)

| # | Bug | Found by | Fix |
|---|---|---|---|
| B1 | Services could not import `nimbus_common`; all bound port 8000 | first `make up` | `PYTHONPATH`, per-service `PORT` |
| B2 | orders-svc had `cache=None` and SQL as comments; worker queue was in-memory and never fed; no OTel export | reading the code | real psycopg pool, Redis, SQL, Postgres job queue, OTLP -> Tempo |
| B3 | Declined cards (402) surfaced as gateway 504s, counting against availability | baseline SLI at 98% | gateway passes 4xx through |
| B4 | `route` label used raw paths: one Prometheus series per order id | query output | label by route template |
| B5 | Freshness SLI = completed/completed -> 0/0 (no alert) exactly when the worker stalls | reasoning about F-06 | denominator = orders created |
| B6 | Freshness rule matched `le="60"`; the bucket label is `le="60.0"` | SLI read 0 | fixed label |
| B7 | k6 diurnal profile ended after 30 minutes | traffic vanished mid-trial | repeat for 24h |
| B8 | Seed overflowed int (`g*7919`); init aborted before roles were created | Postgres logs | bigint arithmetic |
| B9 | Synchronised cache expiry (all 200 SKUs warmed together, same TTL) -> miss storm every 5 min, 5m latency SLI ~0.92 | baseline never "healthy" | +/-30% TTL jitter |
| B10 | uvicorn plain-text access log kept payments-svc "talking" during the F-07 blackout | coverage check saw 907 lines/min | `--no-access-log` (JSON logs already record every request) |

### Fault catalog / injector

| # | Bug | Found by | Fix |
|---|---|---|---|
| B11 | Injector was stubs (chaos/data no-ops, no patches, CLI only printed) | reading | real mechanisms + ground truth to `sherlog_truth` |
| — | **Leak:** source comments name the faults ("THE F-06 FAULT SITE"); any diff hunk touching them hands the agent the answer | reading the first patch | deploy repo holds comment/docstring-stripped code (`ast`); code faults are AST edits, git makes the diff |
| — | Fault commits would add ~70 commits to `master` | design | separate gitignored deploy repo |
| — | F-03 pool 20->3 never saturates at ~13 rps | trial: 0 waiters | pool 20->1 |
| — | F-02 maxmemory 8mb: 13.5x burn, just under the 14.4x fast-burn threshold, never detected | trial | 4mb |
| — | F-07 stopped only log shipping; payments-svc's own metrics/traces still named the cause, so abstaining would be *wrong* | reasoning | full telemetry blackout (`TELEMETRY=off`) + latency toxic |
| — | `injector` role lacked sequence grant | first ground-truth write | grant |

### Engine

| # | Bug | Found by | Fix |
|---|---|---|---|
| E1 | Assessor credited shared symptoms to every hypothesis; posterior stopped discriminating | F-03 run 1 | assessor sees rivals; later one **joint** assessment call (E8) |
| E2 | Model expected config changes in `git_diff_since` | F-03: correct hypothesis refuted | prompt states where config vs code changes show |
| E3 | rises/falls compared window means; a step change is diluted | F-03 | compare max(mean, last) / min(mean, last) |
| E4 | Number extractor read `0.22s` as `0` -> false claim strip | F-03 | unit-aware regex |
| E5 | Normalisation manufactured confidence when every hypothesis was refuted (least-bad -> 0.86) | F-03 run 2 | residual "none of the above" hypothesis |
| E6 | Inverted predictions ("no increase" written as `rises`) | F-03 run 2 | `stable` condition; later `negate` flag (E7) |
| E7 | A hypothesis predicting "no deploy" penalised itself when none was found | F-03 run 3 | `negate` on every prediction |
| E8 | Mini-model assessor confused "refutes a rival" with "refutes this" | F-01 | one joint call, balanced model, explicit rule |
| E9 | Triage never asked "what changed?" | F-01 replay | snapshot adds runtime_state for every service + deploys |
| E10 | Detection fires on the restart blip, before rate() windows show the mechanism | F-01 | 60s settle after detection |
| E11 | Baseline window anchored to detection overlapped the previous fault | F-03 | window anchored to estimated onset |
| E12 | Generic critic dissent ("could be traffic") capped every correct answer | F-01 | dissent must register checkable predictions; all absent -> overruled by code |
| E13 | Duplicate hypotheses split the posterior to zero margin | F-02 | merge at registration (refined in E16) |
| E14 | Evidence gaps only recorded if a hypothesis happened to query the dark service | F-07 | deterministic coverage check in triage |
| E15 | Confident cause named inside a component with no telemetry | F-07 | gate: leader in a dark component -> INSUFFICIENT_EVIDENCE(evidence_gap) |
| E16 | Coverage check called a *quieter* service dark (F-06 worker at 10% volume) and blocked a correct answer | matrix 1, F-06 | dark = at most max(5, 1% of baseline) lines/min, or dead scrape |
| E17 | Duplicate merge required the same fault class; "maxmemory lowered" (config) and "memory exhaustion" (resource) split 0.456/0.453 | matrix 1, F-02 | same component + >=2 shared positive checks |
| — | `proximate_service` instruction said "where users feel it", model always answered gateway | F-02/04/05 | instruction states the catalog's definition (SCORING.md revision) |
| — | Tool budget 40 < worst case 43 | arithmetic | 60 |
| E18 | **O1, real cause.** Each hypothesis's weight went through its own sigmoid, then normalisation. Sigmoids saturate: +10.1 and +6.4 nats both became ~1.0 and normalised to 0.456 / 0.453, throwing away a ~40:1 likelihood ratio. Not the per-kind cap, as first diagnosed | matrix 2, F-02 x3, F-03 x1 (ledger dumps) | posterior = softmax over total weights, "none of the above" at weight 0 (Bayes over exclusive hypotheses, uniform prior). Guards unchanged |
| E19 | F-07 rep2 had abstained only because of E18: "orders-svc pool saturation due to slow payment calls" led while payments-svc was dark; with correct arithmetic it would have answered | offline rescore of matrix 2 | evidence-gap gate also fires when anything the leader's component calls is dark (`DEPENDS_ON` in config.py) |

### Harness

| # | Bug | Found by | Fix |
|---|---|---|---|
| B12 | UI server reading a run file while the engine atomically renamed it -> `PermissionError` on Windows, investigation crashed | matrix 1, F-02 | `save()` retries rename, never raises; UI kept off during matrix |
| B13 | One Prometheus timeout killed the whole runner | dev5 | retries; `healthy()` false when Prometheus unreachable |
| B14 | `healthy()` treated "no data" as healthy; a fault was injected into a system with no traffic | dev5 F-06 | requires traffic, all targets up, every SLI present |
| B15 | Killing the runner mid-reset left orders-svc down; next reset saw clean code and did nothing | dev5 | reset recreates any non-running app container; never kill mid-reset |
| B16 | Network drops (twice) -> provider_unavailable runs counted as results | dev7 | non-budget errors go to errors file; up to 2 retry passes |
| B17 | Scorer: F-01 and F-03 share component and class, so a cache-TTL answer scored correct on the pool fault | dev F-03 | mechanism regex required (SCORING.md revision) |
| — | Heredoc-written edits turned `\n` into newlines / `\b` into backspace, and one empty-match replace inserted text between every character | self | file tools for multi-line edits; signals.py rewritten |

---

## 4. Open issues (not fixed, reported as such)

| # | Issue | Evidence | Likely fix |
|---|---|---|---|
| O1 | **FIXED in `1f7ce70` (E18), see section 7.** Original diagnosis kept for the record: **Cap saturation makes well-supported rivals indistinguishable.** Two hypotheses that both collect strong support hit the per-kind caps (+2.0/kind) and land at near-identical posteriors, so the margin rests only on the few discriminating checks. Seen as a symptom-level hypothesis tying the cause (F-02: "orders-svc pool saturation" 0.453 vs Redis maxmemory 0.456) and as two config hypotheses in one service tying (F-03 rep1 off: pool 0.469 vs cache-TTL 0.437) | matrix 2, F-02 (most reps), F-03 | explains-away rule (support from checks a rival also predicts, where the rival explains strictly more, does not count); or weight discriminating checks outside the cap |
| O8 | **F-03 detectability is load-dependent.** Injected at the diurnal trough (~10 rps), a 1-connection pool (~13 rps capacity at ~75 ms holds) keeps up: 0 waiters, SLIs back to 1.00 after the restart blip, never detected. At 13-16 rps it saturates within a minute | matrix 2, run 35 (F-03 rep3 off) | realistic (capacity faults show at peak); either inject capacity faults at a known load phase, or have the eval record the traffic level at injection and report detection per load band |
| O7 | **Engine failures were classified as infrastructure and retried** (survivorship bias). Rule "any non-budget error -> harness error, retry" caught "hypothesize produced 2 distinct checkable hypotheses after a re-ask" (F-06 rep2 off), an engine outcome | matrix 2, run 25 | in the reported numbers, engine-originated errors count as scored failures (abstention, reason `no_distinct_hypotheses`) on the first attempt; the retry is listed, not substituted. Runner fix: only network/provider/persistence/tool errors retry |
| O2 | Statements that omit the mechanism ("broke job completion") score 0 under the strict scorer. Did not recur in matrix 3 (F-06 6/6) with nothing changed, so this is variance; still open | matrix 2, F-06 rep1 | prompt: name the specific code path / setting changed |
| O3 | `proximate_service` is ambiguous on F-04 (orders-svc saturates first, catalog says payments-svc). Matrix 3: 0/6 again; reports name `psp-stub` (4) or `orders-svc` (2) | dev F-04, matrix 3 | left as defined; reported |
| O4 | `eval/replay.py` cannot replay `runtime_state` (Docker has no history) | dev | dev-only tool; never report replay numbers |
| O5 | No LLM judge: causal-chain F1 not reported | design cut | judge + 20% hand-scored sample |
| O6 | Doc retrieval is BM25 only; Drain3 novelty is vs the pre-onset baseline, not 7 days | design cut | pgvector hybrid; continuous template store |
| O9 | **k6 load shape is not what its comment says.** `load/checkout.js` describes a 30-minute synthetic day; since B7 the code makes ONE sine over the whole 24h run (phase = i/720), so load drifts 0.6x-1.6x over a day and each run's load depends on how long k6 has been up | reading during the O1 rerun | either fix the phase (`i % 15`) and accept a testbed change, or record load at injection (with O8) |
| O10 | **F-02 service localisation is 0/6, in matrix 2, the rerun and matrix 3 alike.** The report names `redis` as `proximate_service`; the catalog's answer is `orders-svc` (the first service whose own behaviour degrades). Same ambiguity as O3 | matrix 2 + rerun + matrix 3 | include the definition's worked example for cache faults in the report prompt |
| O11 | Critic dissent caps most answers at LIKELY (7 of 12 reruns at 0.79; matrix 3: the critic dissented in 29 of 39 runs, and 19 outcomes are LIKELY for that reason alone): the critic still registers alternatives whose predictions are only partly absent | rerun, matrix 3 | report as is; consider overruling when the alternative's predictions are mostly absent |
| O12 | **Model-written claim dates are checked against the evidence windows.** The report schema asks for each claim's full ISO-8601 instant, but evidence content prints only `HH:MM:SSZ`, so the model supplies the date itself. In F-02 rep1 off it wrote 2026-11-21 for a run on 11-27 (times correct). TEMPORAL_MISMATCH stripped all 4 claims, leaving 0 verified, and the run abstained with the right leader at 0.89 / margin 0.78. The check did its job on the claim as written; the date is the fault | matrix 3, F-02 rep1 off | have code fill the date from the investigation window when the model's time-of-day falls inside it; or print full ISO instants in evidence content |
| O13 | **One failed prediction cancels a whole capped evidence kind.** Support from one kind is clamped at +2.0, and an absent prediction costs −2.0 outside the cap. In F-04 rep1 on, the right leader had 4 metric STRONG_SUPPORT (capped to +2.0), git +0.5, trace −0.5 and one absent trace prediction (−2.0): net 0.0, exactly the residual's weight, so posterior 0.50 and abstain. Working as designed (a hypothesis wrong about itself loses its support), but the size of the penalty relative to the cap was never tuned | matrix 3, F-04 rep1 on; also F-05 rep3 off (two absent predictions, leader −3.5, residual wins) | report as is; consider scaling the penalty by how specific the prediction was, or accepting a partly-present check (predicted trace dominance vs observed share) |
| O14 | **Symptom named as cause, at near-certainty.** F-04 rep3 on led with "Postgres connection pool saturation in orders-svc" (0.99). The PSP hypothesis collected no support. The critic dissented, which capped the outcome at LIKELY 0.79, but the answer was given and is wrong. This was the retry of a run whose first attempt hit a provider connection error, scored per O7's rule (infrastructure errors retry; the retry counts) | matrix 3, F-04 rep3 on | hypothesize prompt: a resource-saturation hypothesis must name what started holding the resource longer; or make the critic's dissent decisive when it names a concrete upstream cause |

---

## 5. Matrix 2 outcome, per fault (RCA@1)

Matrix 3's per-fault table is in section 8.

| Fault | Score | Misses and why |
|---|---|---|
| F-01 cache TTL -> 0 | 6/6 | — |
| F-05 N+1 regression (+ decoy commit) | 6/6 | — |
| F-04 slow payment provider | 5/6 | 1 abstention, low margin |
| F-03 pool -> 1 | 4/6 | 1 abstention (O1, TTL vs pool tie); 1 not detected at the traffic trough (O8) |
| F-06 dead-letter branch removed | 3/6 | 2 vague mechanism wording (O2); 1 engine failure, fewer than 3 distinct hypotheses (O7) |
| F-02 Redis maxmemory | 3/6 | 3 abstentions, rival ties under the evidence caps (O1) |
| F-07 telemetry blackout | abstained 3/3; full credit 2/3 | 1 run named the wrong proximate service |

Scored-wrong answers on solvable faults: 2 of 36 (both F-06, O2). Every other miss is an
abstention or a non-detection.

**Highest-value next fixes, in order (revised after matrix 3):** ~~O1~~ (fixed, section 7),
O12 (claim dates filled by code, cheap, and it cost a correct answer), O3 / O10
(proximate-service definition with worked examples, the weakest metric), O13 (penalty vs cap),
O14 (symptom-as-cause), O8 + O9 (record load at injection; fix or document the load shape),
O2 (mechanism specificity).

## 6. Cost

Development runs ~$2.5; aborted first matrix ~$0.7; clean matrix 2 $3.67; O1 rerun (12 runs) $1.11; matrix 3 $3.80. LangSmith free tier.

## 7. O1 fix and rerun (2026-11-27)

**Offline check first.** `python -m eval.rescore` re-applies the posterior and outcome gate to
the stored matrix-2 investigations, no model calls. Exact for the outcome (the posterior
never steers which evidence is gathered); critic/report text is reused as written. Result on
the 36 runs: 4 misses become correct answers (F-02 rep1 on, rep2 on, rep2 off; F-03 rep1
off), 0 regress, F-07 still abstains 3/3 (after E19).

**Live rerun.** All 12 F-02 / F-03 keys, same harness and order, results in
`runtime/eval/rerun-o1.jsonl` (log `rerun-o1.log`), 07:07 -> 09:25 UTC. Lines are stamped
`61d8703-dirty`: the runner started before the commit; the code is exactly `1f7ce70`.

| Fault | RCA@1 matrix 2 | RCA@1 rerun | Service | Origin | Outcomes (rerun) |
|---|---|---|---|---|---|
| F-02 Redis maxmemory | 3/6 | **6/6** | 0/6 (O10) | 6/6 | CONFIRMED 1, LIKELY 4, PARTIALLY_VERIFIED 1 |
| F-03 pool -> 1 | 4/6 | **6/6** | 6/6 | 5/6 | CONFIRMED 3, LIKELY 3 |

Claims stripped 1/62 (1.6%); MTTD 62 +/- 45 s; MTTR-diagnosis 108 s mean; $0.09/run.

**Read with care:**
- **Load differed.** k6 was restarted before the rerun (its 24h profile was about to end), so
  the rerun ran at rising load (~11-14 checkout rps) where matrix 2 ran from peak down to
  trough (O9). F-03 rep3 off was detected this time; that is the load (O8), not the O1 fix.
  The effect of the fix alone is what the offline rescore measures: all 4 tie misses recover
  on their original, unchanged evidence.
- **n is small.** 6 runs per fault; these are not merged into docs/RESULTS.md, which stays the
  record of the matrix on `71fd514`. The clean comparison, a full matrix on the fixed engine, is section 8.
- F-03 rep3 off scored RCA 1.0 with origin wrong: the leading hypothesis named orders-svc, the
  report wrote `originating_component: unknown`.

## 8. Matrix 3: the full matrix on the fixed engine (2026-11-27)

All 39 keys on `0a07fae` (code identical to `1f7ce70`; `0a07fae` only adds this log's section 7),
same harness, order and scoring. Results in `runtime/eval/matrix3.jsonl` (log `matrix3.log`,
report `runtime/eval/matrix3-report.md`), 09:44 -> 19:39 UTC. Not one line is `-dirty`. One
provider connection error (F-04 rep3 on, triage stage) was retried on pass 2, and the retry is
what is scored. Published as the headline of docs/RESULTS.md, with matrix 2 kept below it.

| Fault | Matrix 2 | Matrix 3 | Service | Origin | Outcomes (matrix 3) | Misses and why |
|---|---|---|---|---|---|---|
| F-01 cache TTL -> 0 | 6/6 | 6/6 | 6/6 | 6/6 | CONFIRMED 1, LIKELY 1, PARTIALLY_VERIFIED 4 | — |
| F-03 pool -> 1 | 4/6 | **6/6** | 6/6 | 4/6 | CONFIRMED 1, LIKELY 5 | — |
| F-06 dead-letter removed | 3/6 | **6/6** | 6/6 | 6/6 | LIKELY 3, PARTIALLY_VERIFIED 3 | — |
| F-05 N+1 regression | 6/6 | 5/6 | 6/6 | 6/6 | CONFIRMED 1, LIKELY 3, PARTIALLY_VERIFIED 1, INSUFFICIENT 1 | rep3 off: right leader refuted below the residual, two absent predictions (O13) |
| F-02 Redis maxmemory | 3/6 | 4/6 | 0/6 (O10) | 5/6 | CONFIRMED 2, LIKELY 2, INSUFFICIENT 2 | rep1 off: every claim stripped for a model-guessed date (O12); rep2 off: genuine tie with a "traffic surge" rival at identical weight (+3.4 each) |
| F-04 slow payment provider | 5/6 | 4/6 | 0/6 (O3) | 5/6 | LIKELY 5, INSUFFICIENT 1 | rep1 on: right leader netted exactly 0, tying the residual (O13); rep3 on: **wrong**, symptom as cause (O14) |
| F-07 telemetry blackout | abstained 3/3 | abstained 3/3 | 3/3 | 2/3 | INSUFFICIENT 3 (evidence_gap) | — |

Headline: detection 39/39 (was 38/39); RCA@1 end-to-end 0.86 +/- 0.35, 31/36 (was 0.75, 27/36);
scored-wrong answers 1 of 36 (was 2); claims stripped 15/175 = 8.6% (was 12.0%); CONFIRMED 5
(was 1); MTTD 78 +/- 43 s; MTTR-diagnosis 115 +/- 8 s; $0.10/run, $3.80 total.

**What to credit to what:**
- **The O1 fix: the rival ties.** Matrix 2 lost 4 runs to sigmoid ties. No sigmoid tie recurs.
  The one F-02 tie in matrix 3 is two hypotheses with *identical* weight, which the softmax
  correctly leaves tied. The offline rescore (section 7) is the clean measurement of the fix.
- **Not the fix:** F-06 3/6 -> 6/6 (O2 wording varied, nothing changed), and F-03 rep3 off
  detected (load, O8 / O9; k6 was up since 07:07, so matrix 3 sat on a different part of the
  24h curve and had no run at the trough).
- **New misses, old mechanisms:** O12, O13 and O14 all existed in matrix 2's engine. They
  surfaced here by chance and are not regressions from the fix. The offline rescore showed 0
  regressions on matrix 2's evidence.
- **n:** at ~80% accuracy, one 36-run matrix carries about +/-2.4 runs of binomial noise.
  +4 runs overall is suggestive on its own; the F-02 / F-03 per-fault movement plus the
  offline rescore is the stronger evidence.
