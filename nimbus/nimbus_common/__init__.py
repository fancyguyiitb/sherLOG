"""Shared telemetry, config and HTTP plumbing for every Nimbus service.

Services emit telemetry through this package ONLY. sherLOG reads Loki, Prometheus, Tempo
and Postgres, and never imports from here. If that boundary holds, pointing sherLOG at a
different system is a configuration change rather than a rewrite.
"""
