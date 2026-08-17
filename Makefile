.PHONY: help up down logs test lint catalog inject surprise eval-frozen eval-nightly report clean

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
	@echo "  make eval-frozen    frozen-mode eval — cached responses, \$$0, runs in CI"
	@echo "  make eval-nightly   replay-mode smoke over the 6 faults (~\$$1.50)"
	@echo "  make report         rebuild the shareable project-report PDF"

up:
	docker compose up -d --build
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
	python -m pytest tests/ -v

lint:
	ruff check sherlog faults nimbus tests
	mypy sherlog

catalog:
	python -m faults.injector.cli list

inject:
	python -m faults.injector.cli inject $(F)

surprise:
	python -m faults.injector.cli surprise

eval-frozen:
	SHERLOG_MODE=frozen python -m eval.run --all

eval-nightly:
	SHERLOG_MODE=replay python -m eval.run --faults F-01,F-02,F-03,F-04,F-05,F-06 --reps 1

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
