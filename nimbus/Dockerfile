# One image definition for all five services; SERVICE picks the entrypoint. Five near-
# identical Dockerfiles would drift, and a service whose base image differs from its
# neighbours produces telemetry differences that look like signal.
#
# The image carries dependencies only. Application code is bind-mounted from the deploy
# repo (runtime/deploy-repo), so a "deploy" is a real git commit in that repo followed by a
# container recreate — the same shape as a production rollout, at demo speed.

FROM python:3.12-slim

ARG SERVICE
ENV SERVICE=${SERVICE} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/nimbus

WORKDIR /app

RUN pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.30" "httpx>=0.27" \
      "prometheus-client>=0.20" "pydantic>=2.9" \
      "psycopg[binary,pool]>=3.2" "redis>=5.1" \
      "opentelemetry-sdk>=1.27" "opentelemetry-exporter-otlp-proto-grpc>=1.27" \
      "opentelemetry-instrumentation-fastapi>=0.48b0" \
      "opentelemetry-instrumentation-httpx>=0.48b0"

# Baked-in fallback copy; the bind mount in docker-compose.yml shadows it at runtime.
COPY nimbus/ ./nimbus/

EXPOSE 8000-8004

CMD ["sh", "-c", "uvicorn nimbus.${SERVICE}.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
