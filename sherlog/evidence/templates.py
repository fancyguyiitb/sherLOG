"""Log templating and novelty — the deterministic half of ``log_stats``.

Log volume is the risk most likely to kill this project: 13 rps x ~6 lines/request is
roughly 6.7M lines/day, and no context window survives contact with that. The answer is
statistics before sampling. Cluster lines into templates, show the agent

    template T-014: 4,200 occurrences, up 340% vs baseline
    template T-091: 12 occurrences, never seen before

and let it ask for samples of the specific templates that matter. Novel-template detection
alone catches a surprising share of incidents — and it is the primary signal for F-06,
where the worker's crash handler emits a log line that has never appeared before.

**Templating runs continuously, not on demand.** A background job pulls the last minute
from Loki every minute and maintains a 7-day rolling template table. So ``log_stats`` at
investigation time is a table read rather than a batch job, and ``novel: true`` means
"absent from seven days of history" — a claim we can stand behind. Doing this inside the
investigation would add tens of seconds to MTTD and would make novelty depend on how much
history we happened to fetch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

#: A template counts as novel if it is absent from this much prior history.
NOVELTY_WINDOW = timedelta(days=7)

#: Below this count, a "surge" is noise. Percentage deltas on tiny numbers are meaningless
#: and would flood the agent with 900%-increase templates that went from 1 to 10.
MIN_COUNT_FOR_DELTA = 20


@dataclass(frozen=True, slots=True)
class TemplateStat:
    """One row of what ``log_stats`` returns."""

    template_id: str
    pattern: str
    count: int
    baseline_count: int
    first_seen: datetime
    last_seen: datetime
    novel: bool
    delta_pct: float | None

    def describe(self) -> str:
        if self.novel:
            return f"{self.template_id}: {self.count} occurrences, NEVER SEEN BEFORE — {self.pattern}"
        if self.delta_pct is None:
            return f"{self.template_id}: {self.count} occurrences (below delta floor) — {self.pattern}"
        arrow = "up" if self.delta_pct >= 0 else "down"
        return (
            f"{self.template_id}: {self.count} occurrences, {arrow} "
            f"{abs(self.delta_pct):.0f}% vs 24h baseline — {self.pattern}"
        )


def is_novel(first_seen: datetime, window_start: datetime) -> bool:
    """A template is novel when it first appeared inside the incident window itself."""
    return first_seen >= window_start


def delta_pct(count: int, baseline_count: int) -> float | None:
    """Percentage change against the same window 24h prior.

    Returns None when neither window clears the floor — reporting "+900%" on a template
    that went from 1 to 10 occurrences trains the agent to chase noise.
    """
    if count < MIN_COUNT_FOR_DELTA and baseline_count < MIN_COUNT_FOR_DELTA:
        return None
    if baseline_count == 0:
        return float("inf") if count else 0.0
    return (count / baseline_count - 1.0) * 100.0


def rank(stats: list[TemplateStat], limit: int = 25) -> list[TemplateStat]:
    """Order templates by investigative interest, not by raw frequency.

    Novel templates first — a line that has never been emitted before is the strongest
    single signal in the log plane. Then by magnitude of change. Raw count is the *worst*
    ranking available: the highest-count template is almost always a healthy access log.
    """

    def key(s: TemplateStat) -> tuple[int, float, int]:
        change = abs(s.delta_pct) if s.delta_pct not in (None, float("inf")) else 1e9
        return (1 if s.novel else 0, change, s.count)

    return sorted(stats, key=key, reverse=True)[:limit]
