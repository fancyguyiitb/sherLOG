.PHONY: help venv up down logs test lint catalog inject surprise reset detect ui eval eval-smoke report clean

PY ?= .venv/Scripts/python
CHROME ?= /c/Program Files/Google/Chrome/Application/chrome.exe

help:
	@echo "sherLOG — a verifiable AI SRE"
	@echo ""
	@echo "  make up             start the target system + observability + load"
	@echo "  make down           stop everything"
	@echo "  make test           run the deterministic test suite (\$$0, no LLM calls)"
	@echo "  make lint           ruff + mypy"
	@echo "  make catalog        list the fault catalog"
	@echo "  make inject F=F-04  inject a fault and its declared decoys"
	@echo "  make surprise       inject a random fault without printing which"
	@echo "  make reset          revert every fault; back to the clean baseline"
	@echo "  make detect         run the autonomous detector + investigation loop"
	@echo "  make ui             the glass-box UI on http://localhost:8080"
	@echo "  make eval           the full 39-run matrix (~8h)"
	@echo "  make eval-smoke     one run per fault, decoys on"
	@echo "  make report         rebuild the shareable project-report PDF"

venv:
	python -m venv .venv && $(PY) -m pip install -e ".[dev]"

up:
	$(PY) -c "from faults.injector import deploy; print('deploy repo at', deploy.reset_repo())"
	COMMIT_SHA=$$(git -C runtime/deploy-repo rev-parse --short=7 HEAD) docker compose up -d --build
	@echo "gateway    http://localhost:8000"
	@echo "prometheus http://localhost:9090"
	@echo "grafana    http://localhost:3000  (for us, not part of the product)"
	@echo "toxiproxy  http://localhost:8474  (the chaos injection surface)"

down:
	docker compose down

logs:
	docker compose logs -f gateway orders-svc payments-svc worker

# Everything here is deterministic and free: the redaction gate, the posterior arithmetic,
# the citation verifier, the burn-rate detector, the outcome gate, the catalog schema and
# the architectural boundaries. Those components are most of the thesis, so they get real
# coverage on every push rather than a nightly that costs money and is therefore skipped.
test:
	$(PY) -m pytest tests/ -v

lint:
	ruff check sherlog faults nimbus tests
	mypy sherlog

catalog:
	$(PY) -m faults.injector.cli list

inject:
	$(PY) -m faults.injector.cli inject $(F)

surprise:
	$(PY) -m faults.injector.cli surprise

reset:
	$(PY) -m faults.injector.cli reset

detect:
	$(PY) -m sherlog.detect.service

ui:
	$(PY) -m ui.server

eval:
	$(PY) -m eval.run

eval-smoke:
	$(PY) -m eval.run --reps 1 --decoys on --abstain-reps 1 --out runtime/eval/smoke.jsonl

# The report is checked in as HTML and rendered to PDF on demand. Committing only the PDF
# would make it a dead artifact — unreviewable in a diff and unfixable without whoever
# happened to have the source on their laptop.
report:
	"$(CHROME)" --headless=new --disable-gpu --no-pdf-header-footer \
	  --print-to-pdf="$(CURDIR)/sherLOG_Project_Report.pdf" \
	  --virtual-time-budget=10000 "file://$(CURDIR)/docs/report/report.html"

clean:
	docker compose down -v
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
