"""The seven evidence tools — real implementations against Loki, Prometheus, Tempo, the
deployments table + deploy repo, the Docker API, and the runbook corpus.

Contract, for every tool:

*   Deterministic. No model anywhere in this module.
*   Time windows come from the investigation, never from the model. The model chooses
    *what* to look at; the engine fixes *when*. That makes evidence IDs stable across
    subagents and makes temporal fabrication structurally impossible at retrieval time.
*   Every call returns an ``Evidence`` (content-addressed, unredacted) plus a small typed
    ``facts`` dict that the mechanical signal checker reads. The model only ever sees the
    redacted text; the checker only ever reads the facts. Neither reads the other's input.
*   "No data" is reported as no data, distinctly from "the signal is absent". A service
    that emits nothing has not shown that a predicted signal is missing — it has shown an
    evidence gap, and the engine treats those differently (see ``signals.py``).
"""

from __future__ import annotations

import json
import math
import re
import statistics
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import psycopg
from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig

from ..config import COMPONENTS, SETTINGS
from ..models import Evidence, EvidenceKind
from .series import downsample

_http = httpx.Client(timeout=20.0)
EP = SETTINGS.endpoints

MAX_SAMPLE_LINES = 50
MAX_SERIES = 8


@dataclass(frozen=True)
class Window:
    """The incident window and the equal-length baseline immediately before it."""

    start: datetime
    end: datetime

    @property
    def length(self) -> timedelta:
        return self.end - self.start

    @property
    def baseline(self) -> "Window":
        gap = timedelta(seconds=30)  # a short gap between baseline and onset
        return Window(self.start - gap - self.length, self.start - gap)

    @property
    def range_str(self) -> str:
        return f"{int(self.length.total_seconds())}s"


@dataclass
class ToolResult:
    evidence: Evidence
    facts: dict[str, Any] = field(default_factory=dict)
    no_data: bool = False


def _ev(kind: EvidenceKind, tool: str, args: dict[str, Any], w: Window, content: str,
        source: str) -> Evidence:
    query = f"{tool}({json.dumps(args, sort_keys=True)})"
    return Evidence(kind=kind, query=query, t_start=w.start, t_end=w.end, content=content,
                    source_ref=source)


def _ts(dt: datetime) -> float:
    return dt.timestamp()


def _fmt(v: float) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "n/a"
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 1:
        return f"{v:.2f}"
    return f"{v:.4f}"


# =============================================================================== Prometheus

def _prom_range(promql: str, w: Window, step: int = 15) -> list[dict[str, Any]]:
    r = _http.get(f"{EP.prometheus}/api/v1/query_range",
                  params={"query": promql, "start": _ts(w.start), "end": _ts(w.end),
                          "step": step})
    body = r.json()
    if body.get("status") != "success":
        raise ValueError(body.get("error", "prometheus query failed"))
    return body["data"]["result"]


def _values(series: dict[str, Any]) -> list[tuple[datetime, float]]:
    out = []
    for t, v in series.get("values", []):
        f = float(v)
        if not math.isnan(f) and not math.isinf(f):
            out.append((datetime.fromtimestamp(float(t), timezone.utc), f))
    return out


def metric_query(promql: str, w: Window) -> ToolResult:
    """PromQL over the incident window and the baseline window; shape and change only."""
    args = {"promql": promql}
    try:
        cur = _prom_range(promql, w)
        base = _prom_range(promql, w.baseline)
    except Exception as exc:  # a malformed query is a result the model should see
        ev = _ev("metric", "metric_query", args, w, f"query error: {exc}", "prometheus")
        return ToolResult(ev, {"error": str(exc)}, no_data=True)

    base_by_labels = {json.dumps(s["metric"], sort_keys=True): s for s in base}
    lines, facts_series = [], []
    for s in cur[:MAX_SERIES]:
        key = json.dumps(s["metric"], sort_keys=True)
        pts = _values(s)
        bpts = _values(base_by_labels.get(key, {}))
        if not pts:
            continue
        vals = [v for _, v in pts]
        bvals = [v for _, v in bpts]
        cur_mean, cur_max, last = statistics.fmean(vals), max(vals), vals[-1]
        base_mean = statistics.fmean(bvals) if bvals else float("nan")
        ratio = (cur_mean / base_mean) if bvals and base_mean else float("nan")
        peak_at = max(pts, key=lambda p: p[1])[0]
        shape = " ".join(_fmt(v) for _, v in downsample(pts, 12))
        labels = {k: v for k, v in s["metric"].items() if k != "__name__"} or {"series": "value"}
        lines.append(
            f"{labels}: window mean={_fmt(cur_mean)} max={_fmt(cur_max)} (at "
            f"{peak_at:%H:%M:%S}Z) last={_fmt(last)} | baseline mean={_fmt(base_mean)} | "
            f"ratio vs baseline={_fmt(ratio)}\n  shape: {shape}"
        )
        facts_series.append({"labels": labels, "mean": cur_mean, "max": cur_max, "last": last,
                             "baseline_mean": base_mean, "ratio": ratio})
    if not facts_series:
        ev = _ev("metric", "metric_query", args, w,
                 f"NO DATA for `{promql}` in window {w.start:%H:%M:%S}-{w.end:%H:%M:%S}Z",
                 "prometheus")
        return ToolResult(ev, {"series": []}, no_data=True)
    header = (f"metric_query `{promql}` window {w.start:%H:%M:%S}-{w.end:%H:%M:%S}Z, "
              f"baseline {w.baseline.start:%H:%M:%S}-{w.baseline.end:%H:%M:%S}Z "
              f"({len(cur)} series{', first ' + str(MAX_SERIES) if len(cur) > MAX_SERIES else ''})")
    ev = _ev("metric", "metric_query", args, w, header + "\n" + "\n".join(lines), "prometheus")
    return ToolResult(ev, {"series": facts_series})


# ===================================================================================== Loki

def _loki_instant(logql: str, at: datetime) -> list[dict[str, Any]]:
    r = _http.get(f"{EP.loki}/loki/api/v1/query", params={"query": logql, "time": _ts(at)})
    body = r.json()
    if body.get("status") != "success":
        raise ValueError(str(body)[:300])
    return body["data"]["result"]


def _loki_lines(logql: str, w: Window, limit: int) -> list[dict[str, Any]]:
    r = _http.get(f"{EP.loki}/loki/api/v1/query_range",
                  params={"query": logql, "start": int(_ts(w.start) * 1e9),
                          "end": int(_ts(w.end) * 1e9), "limit": limit, "direction": "backward"})
    body = r.json()
    out = []
    for stream in body.get("data", {}).get("result", []):
        for ts, line in stream["values"]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


_VAR_RE = re.compile(r"\b\d+(?:\.\d+)?(?:ms|s)?\b|\b[0-9a-f]{8,}\b")


def _template_counts(service: str, w: Window) -> dict[tuple[str, str, str], int]:
    logql = (f'sum by (level, event, msg) (count_over_time({{service="{service}"}} '
             f'| json | __error__="" [{w.range_str}]))')
    rows = _loki_instant(logql, w.end)
    out: dict[tuple[str, str, str], int] = {}
    for r in rows:
        m = r["metric"]
        key = (m.get("level", ""), m.get("event", ""), m.get("msg", ""))
        out[key] = out.get(key, 0) + int(float(r["value"][1]))
    return out


def _identity_counts(service: str, w: Window) -> dict[tuple[str, str], int]:
    logql = (f'sum by (config_hash, commit_sha) (count_over_time({{service="{service}"}} '
             f'| json | __error__="" [{w.range_str}]))')
    return {(r["metric"].get("config_hash", ""), r["metric"].get("commit_sha", "")):
            int(float(r["value"][1])) for r in _loki_instant(logql, w.end)}


def log_volume(w: Window) -> ToolResult:
    """log_stats(service="*"): log line volume per service, window vs baseline. This is how
    telemetry *coverage* is checked — a service that goes quiet is an evidence gap, and
    gaps must be found by looking, not by hoping a hypothesis happens to query that service."""
    q = 'sum by (service) (count_over_time({service=~".+"} [%s]))' % w.range_str
    cur = {r["metric"].get("service", "?"): int(float(r["value"][1]))
           for r in _loki_instant(q, w.end)}
    base = {r["metric"].get("service", "?"): int(float(r["value"][1]))
            for r in _loki_instant(q, w.baseline.end)}
    # Coverage is about whether the service is emitting NOW: a blackout that began
    # mid-window still leaves the window total high. So also take the last minute.
    q1 = 'sum by (service) (count_over_time({service=~".+"} [1m]))'
    last = {r["metric"].get("service", "?"): int(float(r["value"][1]))
            for r in _loki_instant(q1, w.end)}
    minutes = max(w.length.total_seconds() / 60, 1)
    lines, volumes = [], {}
    for svc in sorted(set(cur) | set(base)):
        c, b, l1 = cur.get(svc, 0), base.get(svc, 0), last.get(svc, 0)
        per_min = b / minutes
        volumes[svc] = {"count": c, "baseline": b, "last_minute": l1,
                        "baseline_per_minute": round(per_min, 1)}
        flag = "  <-- WENT DARK" if per_min >= 20 and l1 <= max(5.0, 0.01 * per_min) else ""
        lines.append(f"  {svc:<18} window={c:<7} baseline={b:<7} last_minute={l1:<6} "
                     f"(baseline ~{per_min:.0f}/min){flag}")
    content = (f"log_stats * (log volume per service) window {w.start:%H:%M:%S}-{w.end:%H:%M:%S}Z "
               f"vs baseline:\n" + "\n".join(lines))
    return ToolResult(_ev("log", "log_stats", {"service": "*"}, w, content, "loki"),
                      {"volumes": volumes})


def log_stats(service: str, w: Window) -> ToolResult:
    """Statistics first, never raw lines: Drain3 templates with counts vs. the baseline
    window, novelty flags, and which config_hash / commit_sha the service ran under."""
    if service in ("*", "all"):
        return log_volume(w)
    args = {"service": service}
    cur = _template_counts(service, w)
    base = _template_counts(service, w.baseline)
    ident_cur = _identity_counts(service, w)
    ident_base = _identity_counts(service, w.baseline)

    if not cur:
        content = (f"log_stats {service}: NO LOG LINES AT ALL in window "
                   f"{w.start:%H:%M:%S}-{w.end:%H:%M:%S}Z "
                   f"(baseline window had {sum(base.values())} lines). This is an evidence "
                   "gap, not evidence that any event is absent.")
        return ToolResult(_ev("log", "log_stats", args, w, content, f"loki:{{service={service}}}"),
                          {"total": 0, "baseline_total": sum(base.values()), "events": {}},
                          no_data=True)

    # Templates are keyed by the exact (level, event) pair — events are a fixed enum — and
    # Drain3 clusters the free-text message *within* each pair, masking variable tokens, so
    # numeric noise folds together while a reworded message becomes a new template.
    miners: dict[tuple[str, str], TemplateMiner] = {}
    tid: dict[tuple[str, str, str], str] = {}
    patterns: dict[str, str] = {}
    for key in sorted(set(cur) | set(base)):
        level, event, msg = key
        if (level, event) not in miners:
            cfg = TemplateMinerConfig()
            cfg.profiling_enabled = False
            miners[(level, event)] = TemplateMiner(config=cfg)
        res = miners[(level, event)].add_log_message(_VAR_RE.sub("<*>", msg) or "-")
        t = f"{event}#{res['cluster_id']}"
        tid[key] = t
        patterns[t] = f"{level} {event}" + (f" :: {res['template_mined']}" if msg else "")

    agg: dict[str, list[int]] = {}
    events: dict[str, dict[str, int]] = {}
    for key, n in cur.items():
        agg.setdefault(tid[key], [0, 0])[0] += n
        events.setdefault(key[1], {"count": 0, "baseline": 0})["count"] += n
    for key, n in base.items():
        agg.setdefault(tid[key], [0, 0])[1] += n
        events.setdefault(key[1], {"count": 0, "baseline": 0})["baseline"] += n

    def interest(item: tuple[str, list[int]]) -> tuple[int, float, int]:
        c, b = item[1]
        novel = 1 if c and not b else 0
        change = abs(c - b) / max(b, 1)
        return (novel, change, c)

    lines = []
    for t, (c, b) in sorted(agg.items(), key=interest, reverse=True)[:20]:
        if c and not b:
            note = "NOVEL (absent from baseline)"
        elif not c:
            note = "DISAPPEARED (present in baseline only)"
        else:
            note = f"{(c / b - 1) * 100:+.0f}% vs baseline" if b else ""
        lines.append(f"  {t} count={c} baseline={b} {note} :: {patterns.get(t, '?')}")

    def ident_str(d: dict[tuple[str, str], int]) -> str:
        return ", ".join(f"config_hash={h} commit_sha={s} ({n} lines)"
                         for (h, s), n in sorted(d.items(), key=lambda x: -x[1])) or "none"

    cur_hashes = {h for h, _ in ident_cur}
    base_hashes = {h for h, _ in ident_base}
    cur_shas = {s for _, s in ident_cur}
    base_shas = {s for _, s in ident_base}
    config_changed = bool(base_hashes) and bool(cur_hashes - base_hashes)
    commit_changed = bool(base_shas) and bool(cur_shas - base_shas)
    content = (
        f"log_stats {service} window {w.start:%H:%M:%S}-{w.end:%H:%M:%S}Z vs baseline "
        f"{w.baseline.start:%H:%M:%S}-{w.baseline.end:%H:%M:%S}Z\n"
        f"total lines: {sum(cur.values())} (baseline {sum(base.values())})\n"
        f"identity in window:   {ident_str(ident_cur)}\n"
        f"identity in baseline: {ident_str(ident_base)}\n"
        f"config_hash changed vs baseline: {'YES' if config_changed else 'no'}; "
        f"commit_sha changed vs baseline: {'YES' if commit_changed else 'no'}\n"
        f"templates (most changed first):\n" + "\n".join(lines)
    )
    facts = {"total": sum(cur.values()), "baseline_total": sum(base.values()),
             "events": events, "config_changed": config_changed,
             "commit_changed": commit_changed}
    return ToolResult(_ev("log", "log_stats", args, w, content, f"loki:{{service={service}}}"),
                      facts)


def log_sample(service: str, event: str, w: Window, k: int = 20) -> ToolResult:
    """At most 50 raw lines for one event. The cap is enforced here, not in a prompt."""
    k = max(1, min(int(k), MAX_SAMPLE_LINES))
    args = {"service": service, "event": event, "k": k}
    logql = f'{{service="{service}"}} | json | event="{event}"'
    rows = _loki_lines(logql, w, k)
    keep = ("ts", "level", "event", "msg", "duration_ms", "trace_id", "commit_sha",
            "config_hash", "cohort", "attrs")
    if not rows:
        content = f"log_sample {service} event={event}: no matching lines in window"
        return ToolResult(_ev("log", "log_sample", args, w, content, f"loki:{service}"),
                          {"count": 0}, no_data=True)
    body = "\n".join(json.dumps({k2: r.get(k2) for k2 in keep if r.get(k2) not in (None, "")},
                                default=str) for r in rows)
    content = f"log_sample {service} event={event} ({len(rows)} lines, newest first):\n{body}"
    trace_ids = [r.get("trace_id") for r in rows if r.get("trace_id")]
    return ToolResult(_ev("log", "log_sample", args, w, content, f"loki:{service}"),
                      {"count": len(rows), "trace_ids": trace_ids[:5]})


# ==================================================================================== Tempo

def _span_breakdown(trace: dict[str, Any]) -> list[tuple[str, str, float]]:
    out = []
    for batch in trace.get("batches", trace.get("resourceSpans", [])):
        svc = "?"
        for attr in batch.get("resource", {}).get("attributes", []):
            if attr["key"] == "service.name":
                svc = attr["value"].get("stringValue", "?")
        for scope in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", [])):
            for sp in scope.get("spans", []):
                dur = (int(sp["endTimeUnixNano"]) - int(sp["startTimeUnixNano"])) / 1e6
                out.append((svc, sp.get("name", "?"), dur))
    return out


def trace_search(service: str, min_duration_ms: int, w: Window) -> ToolResult:
    """Slow exemplar traces for a service, as a span-duration breakdown — not trace JSON."""
    args = {"service": service, "min_duration_ms": int(min_duration_ms)}
    q = f'{{ resource.service.name = "{service}" && duration > {int(min_duration_ms)}ms }}'
    r = _http.get(f"{EP.tempo}/api/search",
                  params={"q": q, "start": int(_ts(w.start)), "end": int(_ts(w.end)),
                          "limit": 20})
    traces = r.json().get("traces", []) if r.status_code == 200 else []
    if not traces:
        content = (f"trace_search {service} duration>{min_duration_ms}ms: no matching traces "
                   f"in window {w.start:%H:%M:%S}-{w.end:%H:%M:%S}Z")
        return ToolResult(_ev("trace", "trace_search", args, w, content, "tempo"),
                          {"count": 0, "dominant": None}, no_data=True)

    traces = sorted(traces, key=lambda t: -t.get("durationMs", 0))[:3]
    totals: dict[str, list[float]] = {}
    lines = []
    for t in traces:
        full = _http.get(f"{EP.tempo}/api/traces/{t['traceID']}").json()
        spans = _span_breakdown(full)
        root = max((d for _, _, d in spans), default=t.get("durationMs", 0)) or 1
        # Leaf-ish spans are the interesting ones; server spans just contain their children.
        top = sorted(spans, key=lambda s: -s[2])[:8]
        lines.append(f"trace {t['traceID']} {t.get('rootServiceName')} "
                     f"{t.get('rootTraceName')} {t.get('durationMs')}ms")
        for svc, name, dur in top:
            lines.append(f"    {svc:<13} {name:<40} {dur:8.1f}ms ({dur / root * 100:4.0f}%)")
        for svc, name, dur in spans:
            if name.startswith(("db.", "cache.", "job.")) or " http send" in name or name.startswith("POST") or name.startswith("GET"):
                totals.setdefault(f"{svc}:{name}", []).append(dur / root)
    share = {k: statistics.fmean(v) for k, v in totals.items()}
    internal = {k: v for k, v in share.items() if k.split(":", 1)[1].startswith(("db.", "cache.", "job."))}
    dominant = max(internal, key=internal.get) if internal else None  # type: ignore[arg-type]
    content = (f"trace_search {service} duration>{min_duration_ms}ms: {len(r.json().get('traces', []))} "
               f"matching, slowest {len(traces)} broken down:\n" + "\n".join(lines))
    if dominant:
        content += f"\ndominant internal span: {dominant} (~{internal[dominant] * 100:.0f}% of trace)"
    return ToolResult(_ev("trace", "trace_search", args, w, content, "tempo"),
                      {"count": len(traces), "dominant": dominant,
                       "dominant_share": internal.get(dominant, 0.0) if dominant else 0.0,
                       "shares": share})


# ====================================================================== deployments + git

def git_diff_since(w: Window, service: str | None = None) -> ToolResult:
    """Deployments since the baseline began, each joined to its real `git show`."""
    since = w.baseline.start - timedelta(minutes=5)
    args = {"service": service or "*"}
    with psycopg.connect(EP.app_db, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT commit_sha, service, deployed_at, author, diff_summary FROM deployments "
            "WHERE deployed_at >= %s AND (%s::text IS NULL OR service = %s) "
            "ORDER BY deployed_at",
            (since, service, service),
        ).fetchall()
    if not rows:
        content = f"git_diff_since {since:%H:%M:%S}Z: no deployments"
        return ToolResult(_ev("git", "git_diff_since", args, w, content, "deployments"),
                          {"deploys": []})
    parts, deploys = [], []
    for sha, svc, at, author, summary in rows:
        show = subprocess.run(
            ["git", "-C", str(EP.deploy_repo), "show", "--format=%h %an: %s", sha],
            capture_output=True, text=True,
        ).stdout
        diff_lines = show.splitlines()[:60]
        parts.append(f"deploy {sha} to {svc} at {at:%H:%M:%S}Z by {author}\n"
                     + "\n".join("    " + ln for ln in diff_lines))
        deploys.append({"sha": sha, "service": svc, "at": at.isoformat()})
    content = f"git_diff_since {since:%H:%M:%S}Z: {len(rows)} deployment(s)\n" + "\n".join(parts)
    return ToolResult(_ev("git", "git_diff_since", args, w, content, "deployments+git"),
                      {"deploys": deploys})


# ==================================================================================== state

_SECRETISH = re.compile(r"URL|PASSWORD|SECRET|KEY|TOKEN|DSN", re.I)
_STATE_KEYS = ("CATALOG_CACHE_TTL_S", "DB_POOL_MAX", "DB_ACQUIRE_TIMEOUT_S", "REDIS_MAXMEMORY",
               "UPSTREAM_TIMEOUT_S", "UPSTREAM_RETRIES", "JOB_MAX_ATTEMPTS", "LOG_LEVEL",
               "TELEMETRY", "COMMIT_SHA")


def runtime_state(service: str, w: Window) -> ToolResult:
    """Container status, restarts, OOM, when it last (re)started, and its non-secret config."""
    args = {"service": service}
    name = f"{EP.compose_project}-{service}-1"
    out = subprocess.run(["docker", "inspect", name], capture_output=True, text=True)
    if out.returncode != 0:
        content = f"runtime_state {service}: container not found"
        return ToolResult(_ev("state", "runtime_state", args, w, content, "docker"),
                          {}, no_data=True)
    info = json.loads(out.stdout)[0]
    state = info["State"]
    started = datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00")[:26] + "+00:00")
    env = dict(e.split("=", 1) for e in info["Config"]["Env"] if "=" in e)
    shown = {k: v for k, v in env.items() if k in _STATE_KEYS and not _SECRETISH.search(k)}
    restarted_in_window = started >= w.baseline.start
    up = "n/a"
    if service in ("gateway", "orders-svc", "payments-svc", "worker"):
        try:
            res = _http.get(f"{EP.prometheus}/api/v1/query",
                            params={"query": f'up{{instance="{service}"}}', "time": _ts(w.end)}).json()
            up = res["data"]["result"][0]["value"][1] if res["data"]["result"] else "absent"
        except Exception:
            up = "error"
    content = (
        f"runtime_state {service}: status={state['Status']} restart_count={info['RestartCount']} "
        f"oom_killed={state['OOMKilled']}\n"
        f"started_at={started:%H:%M:%S}Z ({'WITHIN' if restarted_in_window else 'before'} the "
        f"baseline+incident period starting {w.baseline.start:%H:%M:%S}Z)\n"
        f"prometheus scrape up={up}\n"
        f"config (non-secret, explicitly set): {json.dumps(shown, sort_keys=True)}"
    )
    return ToolResult(_ev("state", "runtime_state", args, w, content, f"docker:{name}"),
                      {"restarted_in_window": restarted_in_window, "config": shown,
                       "up": up, "started_at": started.isoformat()})


# ===================================================================================== docs

_WORD = re.compile(r"[a-z0-9_]+")


def _chunks() -> list[tuple[str, str, str]]:
    out = []
    for path in sorted(SETTINGS.runbooks_dir.glob("*.md")):
        title, heading, buf = path.stem, "", []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                if buf:
                    out.append((path.stem, heading or title, "\n".join(buf).strip()))
                heading, buf = line.lstrip("# ").strip(), []
            else:
                buf.append(line)
        if buf:
            out.append((path.stem, heading or title, "\n".join(buf).strip()))
    return [c for c in out if c[2]]


def doc_search(query: str, w: Window, k: int = 3) -> ToolResult:
    """BM25 over the private runbook corpus, chunked by heading."""
    args = {"query": query}
    chunks = _chunks()
    docs = [_WORD.findall((h + " " + b).lower()) for _, h, b in chunks]
    n = len(docs) or 1
    avg = sum(map(len, docs)) / n
    df: dict[str, int] = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    q = _WORD.findall(query.lower())
    scores = []
    for i, d in enumerate(docs):
        s = 0.0
        for t in q:
            f = d.count(t)
            if not f:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(d) / avg))
        scores.append((s, i))
    top = [chunks[i] for s, i in sorted(scores, reverse=True)[:k] if s > 0]
    body = "\n\n".join(f"[{doc} > {h}]\n{b[:900]}" for doc, h, b in top) or "no matching runbook"
    return ToolResult(_ev("doc", "doc_search", args, w, f"doc_search '{query}':\n{body}",
                          "runbooks"), {"hits": [f"{d} > {h}" for d, h, _ in top]})


# ================================================================================ registry

TOOL_HELP = f"""\
Tools (time windows are fixed by the engine; you choose only the arguments shown):
- metric_query(promql): any PromQL. Returns window vs baseline mean/max/ratio per series.
  Metric names: http_requests_total{{service,route,status,cohort}},
  http_request_duration_seconds_bucket{{service,route,cohort,le}},
  upstream_duration_seconds_bucket{{service,upstream,le}} (upstream in orders-svc, payments-svc, psp),
  upstream_requests_total{{service,upstream,status}}, db_pool_size{{service}}, db_pool_in_use{{service}},
  db_pool_waiters{{service}}, db_pool_acquire_seconds_bucket{{service,le}},
  cache_ops_total{{service,op,result}} (result hit|miss|error), redis_evicted_keys_total,
  redis_memory_used_bytes, redis_memory_max_bytes, redis_keyspace_hits_total, redis_keyspace_misses_total,
  queue_depth{{queue}}, job_oldest_age_seconds{{queue}}, job_attempts_total{{queue,status}}
  (status complete|retry|dead_letter), orders_created_total, order_completion_seconds_bucket,
  pg_stat_database_xact_commit, up{{instance}}. Recorded: sli:availability:ratio_rate30s,
  sli:latency:ratio_rate30s, sli:freshness:ratio_rate30s, sli:latency:ratio_rate5m:by_cohort,
  discriminator:cache_miss_rate, discriminator:redis_eviction_rate, discriminator:psp_latency_p99.
  Routes look like "POST /checkout", "POST /orders", "POST /payments".
- log_stats(service): log templates vs baseline, novelty, config_hash/commit_sha changes.
- log_sample(service, event, k<=50): raw lines for one event name.
- trace_search(service, min_duration_ms): slow traces broken down by span.
- git_diff_since(service|null): deployments in the period with their real diffs.
- runtime_state(service): container restarts, start time, explicitly-set config, scrape status.
- doc_search(query): runbooks and past postmortems.
Services: {", ".join(COMPONENTS)}. Log events include: http.request.end, checkout.failed,
upstream.slow, upstream.timeout, upstream.error, db.acquire.timeout, db.query, cache.error,
order.created, payment.captured, payment.declined, job.claimed, job.complete, job.retry,
job.handler.error, job.dead_lettered, service.start."""


def run_tool(name: str, args: dict[str, Any], w: Window) -> ToolResult:
    """Dispatch by name. Unknown tools and bad arguments raise ValueError."""
    if name == "metric_query":
        return metric_query(str(args["promql"]), w)
    if name == "log_stats":
        return log_stats(str(args["service"]), w)
    if name == "log_sample":
        return log_sample(str(args["service"]), str(args["event"]), w, int(args.get("k", 20)))
    if name == "trace_search":
        return trace_search(str(args["service"]), int(args.get("min_duration_ms", 500)), w)
    if name == "git_diff_since":
        svc = args.get("service")
        return git_diff_since(w, str(svc) if svc not in (None, "", "*", "null") else None)
    if name == "runtime_state":
        return runtime_state(str(args["service"]), w)
    if name == "doc_search":
        return doc_search(str(args["query"]), w)
    raise ValueError(f"unknown tool {name!r}")
