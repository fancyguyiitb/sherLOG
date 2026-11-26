"""The glass-box frontend: live investigation view, fault console, eval table.

The UI observes; it does not drive. The only write actions are on the fault console —
inject and reset — which act on the *target system*, never on the engine. Investigations
are opened by the detector on its own.

    python -m ui.server            # http://localhost:8080
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from faults.injector import mechanisms
from faults.schema import load_catalog

ROOT = Path(__file__).resolve().parents[1]
INV_DIR = ROOT / "runtime" / "investigations"
RESULTS = ROOT / "runtime" / "eval" / "results.jsonl"
CATALOG = load_catalog(ROOT / "faults" / "catalog")

app = FastAPI(title="sherLOG")
_lock = threading.Lock()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).with_name("index.html"))


@app.get("/api/investigations")
def list_investigations() -> list[dict[str, Any]]:
    out = []
    for p in sorted(INV_DIR.glob("inv_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({"id": d["id"], "label": d.get("label"), "started_at": d["started_at"],
                    "stage": d["ledger"]["stage"],
                    "outcome": (d.get("decision") or {}).get("outcome"),
                    "root_cause": (d.get("report") or {}).get("root_cause"),
                    "alerts": d["incident"]["alerts"]})
    return out


@app.get("/api/investigations/{inv_id}")
def get_investigation(inv_id: str) -> dict[str, Any]:
    p = INV_DIR / f"{inv_id}.json"
    if not p.exists() or not inv_id.startswith("inv_"):
        raise HTTPException(404)
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/api/eval")
def eval_results() -> list[dict[str, Any]]:
    if not RESULTS.exists():
        return []
    return [json.loads(line) for line in RESULTS.read_text(encoding="utf-8").splitlines()
            if line.strip()]


@app.get("/api/faults")
def faults() -> list[dict[str, Any]]:
    return [{"id": f.id, "name": f.name, "class": f.fault_class, "slo": f.breaches_slo,
             "decoys": f.decoys} for f in CATALOG.values() if f.id.startswith("F-")]


@app.post("/api/inject/{fault_id}")
def inject(fault_id: str, decoys: bool = True) -> dict[str, Any]:
    fault = CATALOG.get(fault_id)
    if fault is None or not fault_id.startswith("F-"):
        raise HTTPException(404)
    with _lock:
        rec, iid = mechanisms.inject(fault, [CATALOG[d] for d in fault.decoys] if decoys else [])
    return {"injected_at": rec.injected_at.isoformat(), "injection_id": iid}


@app.post("/api/reset")
def reset() -> dict[str, str]:
    with _lock:
        mechanisms.reset_environment()
    return {"status": "baseline"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
