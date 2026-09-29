import json
import os
import re
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from hindsight_client import Hindsight
from pydantic import BaseModel

from extraction import extract_finding
from ingest_to_hindsight import build_content
from interval_check import summarize_findings
from open_loop_check import check_open_loops

BASE_DIR = os.path.dirname(__file__)
INDEX_PATH = os.getenv("FINDINGS_INDEX", os.path.join(BASE_DIR, "data", "findings_index.json"))

load_dotenv()

app = FastAPI(title="LungTrace Dashboard")
index_lock = threading.Lock()
INDEX_FIELDS = ("date", "organ", "location", "finding_type", "size_mm", "recommendation", "follow_up_due_date")


class ProcessReportRequest(BaseModel):
    patient_id: str
    date: str
    report_text: str


def save_index(index: dict) -> None:
    """Write atomically so a crash mid-write can't corrupt the index."""
    tmp = INDEX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
    os.replace(tmp, INDEX_PATH)


def build_detail(patient_id: str, findings: list) -> dict:
    history = sorted(findings, key=lambda f: f["date"])
    return {
        "patient_id": patient_id,
        "patient_name": history[0].get("patient_name", patient_id),
        "latest": history[-1],
        "history": history,
        "interval_summary": summarize_findings([dict(f) for f in history]).split("\n"),
        "open_loops": check_open_loops({patient_id: [dict(f) for f in history]}),
    }


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
    findings = load_index().get(patient_id)
    if not findings:
        raise HTTPException(404, f"Unknown patient {patient_id}")
    return build_detail(patient_id, findings)


@app.post("/api/process-report")
def process_report(req: ProcessReportRequest):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", req.date):
        raise HTTPException(422, "Date must be YYYY-MM-DD.")
    try:
        report_dt = datetime.strptime(req.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(422, f"{req.date} is not a valid date.")
    if not req.report_text.strip():
        raise HTTPException(422, "Report text is empty.")

    index = load_index()
    if patient_id_missing(index, req.patient_id):
        raise HTTPException(404, f"Unknown patient {req.patient_id}")
    patient_name = index[req.patient_id][0].get("patient_name", req.patient_id)

    # 1. Extract (live Groq call)
    try:
        finding = extract_finding(req.report_text, req.patient_id, req.date)
    except Exception as e:
        raise HTTPException(502, f"Extraction failed (Groq): {e}")
    if not isinstance(finding.get("size_mm"), (int, float)) or not finding.get("organ") or not finding.get("location"):
        raise HTTPException(422, f"Could not extract a usable finding from that text: {finding}")
    finding["finding_type"] = finding.get("finding_type") or "finding"
    finding["recommendation"] = finding.get("recommendation") or "none stated"

    # 2. Retain in Hindsight (live call); same document_id pattern as ingestion
    try:
        client = Hindsight(base_url=os.getenv("HINDSIGHT_BASE_URL"), api_key=os.getenv("HINDSIGHT_API_KEY"))
        client.retain(
            bank_id=req.patient_id,
            content=build_content(finding),
            timestamp=report_dt,
            document_id=f"{req.patient_id}-{req.date}",
        )
    except Exception as e:
        raise HTTPException(502, f"Saving to Hindsight failed: {e}")

    # 3. Append to local index (only after both live calls succeeded)
    entry = {"patient_name": patient_name, **{k: finding[k] for k in INDEX_FIELDS}}
    with index_lock:
        index = load_index()
        findings = index[req.patient_id]
        findings[:] = [
            f for f in findings
            if not (f["date"] == entry["date"] and f["organ"].lower() == entry["organ"].lower()
                    and f["location"].lower() == entry["location"].lower())
        ]
        findings.append(entry)
        save_index(index)

    # 4. Recompute summary and open loops
    return {**build_detail(req.patient_id, findings), "new_finding": entry}


def patient_id_missing(index: dict, patient_id: str) -> bool:
    return not index.get(patient_id)


SUMMARY_QUERY = (
    "Summarize this patient's imaging findings, the trend over time, "
    "and any follow-up concerns a clinician should know about."
)


@app.get("/api/patients/{patient_id}/summary")
def patient_summary(patient_id: str):
    """On-demand synthesis from Hindsight memory. Deliberately uncached: every call hits Hindsight."""
    if patient_id_missing(load_index(), patient_id):
        raise HTTPException(404, f"Unknown patient {patient_id}")
    try:
        client = Hindsight(base_url=os.getenv("HINDSIGHT_BASE_URL"), api_key=os.getenv("HINDSIGHT_API_KEY"))
        response = client.reflect(bank_id=patient_id, query=SUMMARY_QUERY)
    except Exception as e:
        raise HTTPException(502, f"Hindsight reflect failed: {e}")
    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise HTTPException(502, "Hindsight reflect returned an empty response.")
    return {"patient_id": patient_id, "summary": text}
