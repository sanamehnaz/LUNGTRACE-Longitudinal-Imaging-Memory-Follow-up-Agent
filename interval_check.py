import json
import os
import re
from collections import defaultdict
from datetime import date

from dotenv import load_dotenv
from hindsight_client import Hindsight

load_dotenv()

INDEX_PATH = os.path.join(os.path.dirname(__file__), "data", "findings_index.json")

_client = None


def _get_client() -> Hindsight:
    global _client
    if _client is None:
        _client = Hindsight(
            base_url=os.getenv("HINDSIGHT_BASE_URL"),
            api_key=os.getenv("HINDSIGHT_API_KEY"),
        )
    return _client


def get_patient_timeline(bank_id: str) -> list:
    """Recall all memories for a patient bank; returns dicts with text and timestamps."""
    response = _get_client().recall(bank_id=bank_id, query="all findings for this patient")
    timeline = [
        {
            "content": r.text,
            "occurred_start": r.occurred_start,
            "mentioned_at": r.mentioned_at,
            "document_id": r.document_id,
        }
        for r in response.results
    ]
    timeline.sort(key=lambda m: m["occurred_start"] or m["mentioned_at"] or "")
    return timeline


def _load_index(bank_id: str) -> list:
    """Structured findings written during ingestion (source of truth for sizes/dates)."""
    try:
        with open(INDEX_PATH, encoding="utf-8") as f:
            return json.load(f).get(bank_id, [])
    except FileNotFoundError:
        return []


_PATTERN = re.compile(
    r"On (\d{4}-\d{2}-\d{2}), an? (.+?) was found in the (.+?), (.+?), measuring ([\d.]+)mm"
)


def _parse_from_timeline(timeline: list) -> list:
    """Best-effort fallback: regex-parse findings out of recalled text."""
    findings = []
    for m in timeline:
        match = _PATTERN.search(m["content"])
        if match:
            d, ftype, organ, loc, size = match.groups()
            findings.append({"date": d, "finding_type": ftype, "organ": organ,
                             "location": loc, "size_mm": float(size)})
    return findings


def _fmt(n) -> str:
    return f"{n:g}"


def _months_between(d1: str, d2: str) -> int:
    return round((date.fromisoformat(d2) - date.fromisoformat(d1)).days / 30.44)


def summarize_findings(findings: list) -> str:
    """Pure-Python interval summary from structured findings (no network calls)."""
    if not findings:
        return "No findings found."

    groups = defaultdict(list)
    for f in findings:
        groups[(f["organ"].lower(), f["location"].lower())].append(f)

    lines = []
    for items in groups.values():
        items.sort(key=lambda f: f["date"])
        if len(items) < 2:
            lines.append("No prior findings to compare.")
            continue
        first, last = items[0], items[-1]
        label = f"{first['location']} {first['finding_type'].lower()}"
        chain = " -> ".join(f"{_fmt(f['size_mm'])}mm ({f['date']})" for f in items)
        change = (last["size_mm"] - first["size_mm"]) / first["size_mm"] * 100
        months = _months_between(first["date"], last["date"])
        span = f"{months} months" if months else "under 1 month"
        word = "Growth" if change > 0 else "Shrinkage" if change < 0 else "Change"
        lines.append(f"{label}: {chain}. {word}: {abs(change):g}% over {span}." if change else
                     f"{label}: {chain}. Stable over {span}.")
    return "\n".join(lines)


def generate_interval_summary(bank_id: str) -> str:
    timeline = get_patient_timeline(bank_id)  # exercises Hindsight recall
    findings = _load_index(bank_id) or _parse_from_timeline(timeline)
    return summarize_findings(findings)


if __name__ == "__main__":
    for pid in ["P001", "P002", "P003", "P004", "P005"]:
        print(f"--- {pid} ---")
        try:
            print(generate_interval_summary(pid))
        except Exception as e:
            print(f"ERROR: {e}")
        print()
