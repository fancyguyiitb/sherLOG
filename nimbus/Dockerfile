# One image definition for all five services; SERVICE picks the entrypoint. Five near-
# identical Dockerfiles would drift, and a service whose base image differs from its
# neighbours produces telemetry differences that look like signal.

FROM python:3.12-slim

ARG SERVICE
ENV SERVICE=${SERVICE} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.30" "httpx>=0.27" \
      "prometheus-client>=0.20" "pydantic>=2.9" \
      "opentelemetry-sdk>=1.27" "opentelemetry-exporter-otlp>=1.27" \
      "opentelemetry-instrumentation-fastapi>=0.48b0" \
      "opentelemetry-instrumentation-httpx>=0.48b0"

COPY nimbus/ ./nimbus/

# COMMIT_SHA is baked in at build time and stamped on every log line. It is what makes
# git_diff_since() correlation possible: without it the agent would have to infer which
# code was live from deploy timestamps alone, and F-05 would turn on a guess.
ARG COMMIT_SHA=dev
ENV COMMIT_SHA=${COMMIT_SHA}

# gateway 8000, orders 8001, payments 8002, psp 8003
EXPOSE 8000-8003

CMD ["sh", "-c", "uvicorn nimbus.${SERVICE}.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
