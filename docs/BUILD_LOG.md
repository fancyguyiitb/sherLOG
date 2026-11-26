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
| Eval matrix: 39 runs (6 faults x 3 reps x 2 decoy modes + 3x F-07) | **running on commit `71fd514`**, started 15:22 UTC, ETA ~01:45 UTC |
| Results report | `python -m eval.report` -> `docs/RESULTS.md` after the matrix |

**To resume the matrix if it dies:** `make up` (if the stack is down), then
`.venv/Scripts/python -u -W ignore -m eval.run >> runtime/eval/matrix.log 2>&1`.
It skips every key already in `runtime/eval/results.jsonl`. Do **not** kill it mid-reset
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
| `71fd514` | Duplicate merge ignores fault-class label |

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
| O1 | **Symptom-level hypotheses tie with the cause.** "orders-svc pool saturation" (all-symptom predictions) tied 0.456/0.453 with the correct Redis hypothesis; both hit the per-kind caps | matrix 2, F-02 rep1 | explains-away rule: support from checks a rival also predicts, where the rival explains strictly more, does not count |
| O2 | Statements that omit the mechanism ("broke job completion") score 0 under the strict scorer | matrix 2, F-06 rep1 | prompt: name the specific code path / setting changed |
| O3 | `proximate_service` is ambiguous on F-04 (orders-svc saturates first, catalog says payments-svc) | dev F-04 | left as defined; reported |
| O4 | `eval/replay.py` cannot replay `runtime_state` (Docker has no history) | dev | dev-only tool; never report replay numbers |
| O5 | No LLM judge: causal-chain F1 not reported | design cut | judge + 20% hand-scored sample |
| O6 | Doc retrieval is BM25 only; Drain3 novelty is vs the pre-onset baseline, not 7 days | design cut | pgvector hybrid; continuous template store |

---

## 5. Cost so far

Development runs ~$2.5; aborted matrix ~$0.7; clean matrix ~$4-5 expected. LangSmith free tier.
