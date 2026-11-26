# Runbook: correlating incidents with deploys and config changes

## Deploys
`git_diff_since` lists deployments in the period with their diffs. A deploy near the onset
is a lead, not a verdict: read the diff. A change touching only log message wording or
docs cannot change latency or error behaviour. Reworded log messages show up as NEW log
templates in log_stats — novelty there reflects the wording change, not a fault.
A code change that turns one batched query into a per-item loop scales with basket size:
look at latency split by cohort (`sli:latency:ratio_rate5m:by_cohort`, label cohort
small|large) and at `db.query` events per request.

## Config changes
Every service stamps `config_hash` on every log line; a new hash in the incident window
means its configuration changed (and the container restarted). runtime_state shows
explicitly set config values. Redis config is not in any log line: check runtime_state
for a recent restart and its explicitly set values.
