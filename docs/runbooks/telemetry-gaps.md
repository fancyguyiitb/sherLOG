# Runbook: missing telemetry

## Symptoms
A service stops appearing in logs, traces, or Prometheus (`up{instance=...} == 0`, or
absent), while its callers still see it respond.

## What you can and cannot conclude
You can see the service from the outside: callers' `upstream_duration_seconds` and error
rates. You cannot see inside it. If the service is degraded and its own telemetry is
missing, the cause is not observable: state the proximate service, state the evidence gap,
restore telemetry, and re-investigate. Do not infer an internal cause from silence.
