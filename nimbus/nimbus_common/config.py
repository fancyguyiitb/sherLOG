"""Service configuration, and the config hash that makes config faults diagnosable.

Every service computes a hash over its own effective configuration at startup and stamps
that hash on every log line it emits. When the injector rewrites an env file and recreates
the container, the hash changes — so the agent can answer "did this service's configuration
change, and when?" from a log query alone, without a join and without trusting a deploy
timeline.

That single field is what separates F-01 (orders-svc config moved) from F-02 (Redis's
config moved, orders-svc unchanged), and F-03 (pool config moved) from F-04 (pool config
unchanged, the pool is a victim). Three of the six v1 faults are solvable largely because
of it.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, fields


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    service: str
    port: int = 8000

    # Identity stamped on every log line and every metric exemplar.
    commit_sha: str = field(default_factory=lambda: _env("COMMIT_SHA", "dev")[:7])
    environment: str = field(default_factory=lambda: _env("ENVIRONMENT", "local"))

    # Datastores
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", ""))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", ""))

    # F-01 target
    catalog_cache_ttl_s: int = field(default_factory=lambda: _env_int("CATALOG_CACHE_TTL_S", 300))
    # F-03 target
    db_pool_max: int = field(default_factory=lambda: _env_int("DB_POOL_MAX", 20))
    db_pool_min: int = field(default_factory=lambda: _env_int("DB_POOL_MIN", 2))
    db_acquire_timeout_s: float = field(
        default_factory=lambda: _env_float("DB_ACQUIRE_TIMEOUT_S", 3.0)
    )

    # HTTP client policy — the gateway owns timeouts and retries for the whole path
    upstream_timeout_s: float = field(default_factory=lambda: _env_float("UPSTREAM_TIMEOUT_S", 5.0))
    upstream_retries: int = field(default_factory=lambda: _env_int("UPSTREAM_RETRIES", 1))

    # Worker
    job_max_attempts: int = field(default_factory=lambda: _env_int("JOB_MAX_ATTEMPTS", 5))
    job_poll_interval_s: float = field(
        default_factory=lambda: _env_float("JOB_POLL_INTERVAL_S", 0.5)
    )

    # Telemetry
    otlp_endpoint: str = field(default_factory=lambda: _env("OTEL_EXPORTER_OTLP_ENDPOINT", ""))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    @property
    def config_hash(self) -> str:
        """Hash over the behaviour-affecting configuration only.

        ``commit_sha`` is deliberately excluded: code identity and config identity are
        different questions, and collapsing them would make "the config changed but the
        code did not" — the exact shape of F-01 and F-03 — unanswerable.

        Connection URLs are excluded too, since they carry credentials and would otherwise
        put a password inside a value that is printed on every single log line.
        """
        excluded = {"commit_sha", "database_url", "redis_url", "otlp_endpoint"}
        payload = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name not in excluded
        }
        blob = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:6]

    def as_dict(self) -> dict[str, object]:
        redacted = asdict(self)
        for key in ("database_url", "redis_url"):
            if redacted.get(key):
                redacted[key] = "[configured]"
        redacted["config_hash"] = self.config_hash
        return redacted


def load(service: str, port: int = 8000) -> Settings:
    return Settings(service=service, port=_env_int("PORT", port))
