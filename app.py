import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from interval_check import summarize_findings
from open_loop_check import check_open_loops

BASE_DIR = os.path.dirname(__file__)
INDEX_PATH = os.getenv("FINDINGS_INDEX", os.path.join(BASE_DIR, "data", "findings_index.json"))

app = FastAPI(title="LungTrace Dashboard")


def load_index() -> dict:
    """Read the local index on each request so re-ingesting needs no server restart."""
    try:
        with open(INDEX_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise HTTPException(503, f"{INDEX_PATH} not found. Run ingest_to_hindsight.py first.")


@app.get("/")
def dashboard():
    return FileResponse(os.path.join(BASE_DIR, "templates", "dashboard.html"))


@app.get("/api/patients")
def patients():
    index = load_index()
    return [
        {"patient_id": pid, "patient_name": findings[0].get("patient_name", pid)}
        for pid, findings in sorted(index.items())
    ]


@app.get("/api/patients/{patient_id}")
def patient_detail(patient_id: str):
    index = load_index()
    findings = index.get(patient_id)
    if not findings:
        raise HTTPException(404, f"Unknown patient {patient_id}")
    history = sorted(findings, key=lambda f: f["date"])
    return {
        "patient_id": patient_id,
        "patient_name": history[0].get("patient_name", patient_id),
        "latest": history[-1],
        "history": history,
        "interval_summary": summarize_findings([dict(f) for f in history]).split("\n"),
        "open_loops": check_open_loops({patient_id: [dict(f) for f in history]}),
    }
