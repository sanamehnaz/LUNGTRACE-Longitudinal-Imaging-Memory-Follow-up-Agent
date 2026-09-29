import json
import os
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
from hindsight_client import Hindsight

from extraction import extract_finding

load_dotenv()

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "synthetic_reports.json")
INDEX_PATH = os.path.join(os.path.dirname(__file__), "data", "findings_index.json")


INDEX_FIELDS = ("date", "organ", "location", "finding_type", "size_mm", "recommendation", "follow_up_due_date")


def load_index(path: str = None) -> dict:
    """Existing local index, or {} if it doesn't exist yet."""
    try:
        with open(path or INDEX_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_index(index: dict, path: str = None) -> None:
    """Atomic write so an interrupted run can't corrupt the index."""
    path = path or INDEX_PATH
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
    os.replace(tmp, path)


def merge_finding(index: dict, patient_id: str, entry: dict) -> None:
    """Add or update one finding. Identity is (date, organ, location), so re-ingesting the same report
    updates it in place instead of duplicating it, and findings from other sources are left alone."""
    key = lambda f: (f["date"], f["organ"].lower(), f["location"].lower())
    findings = index.setdefault(patient_id, [])
    findings[:] = [f for f in findings if key(f) != key(entry)]
    findings.append(entry)
    findings.sort(key=lambda f: f["date"])


def build_content(finding: dict) -> str:
    follow_up = finding["follow_up_due_date"] or "none"
    return (
        f"On {finding['date']}, a {finding['finding_type']} was found in the {finding['organ']}, "
        f"{finding['location']}, measuring {finding['size_mm']}mm. "
        f"Recommendation: {finding['recommendation']}. "
        f"Follow-up due: {follow_up}."
    )


def main():
    client = Hindsight(
        base_url=os.getenv("HINDSIGHT_BASE_URL"),
        api_key=os.getenv("HINDSIGHT_API_KEY"),
    )

    with open(DATA_PATH, encoding="utf-8") as f:
        reports = json.load(f)

    by_patient = defaultdict(list)
    for report in reports:
        by_patient[report["patient_id"]].append(report)

    total_retained = 0
    index = load_index()  # merge into what's already there (e.g. live-submitted reports)
    for patient_id, patient_reports in by_patient.items():
        patient_reports.sort(key=lambda r: r["date"])
        patient_name = patient_reports[0]["patient_name"]

        try:
            client.create_bank(bank_id=patient_id, name=patient_name)
            print(f"[bank] created {patient_id} ({patient_name})")
        except Exception as e:
            # Most likely the bank already exists; continue and retain into it.
            print(f"[bank] {patient_id}: could not create ({e}); continuing")

        for report in patient_reports:
            try:
                finding = extract_finding(report["report_text"], report["patient_id"], report["date"])
                content = build_content(finding)
                client.retain(
                    bank_id=patient_id,
                    content=content,
                    timestamp=datetime.strptime(report["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc),
                    document_id=f"{patient_id}-{report['date']}",
                )
                total_retained += 1
                merge_finding(index, patient_id, {"patient_name": patient_name, **{k: finding[k] for k in INDEX_FIELDS}})
                print(f"[retain] {patient_id} {report['date']}: {content[:90]}...")
            except Exception as e:
                print(f"[error] {patient_id} {report['date']}: {e}")

    save_index(index)
    print(f"[index] merged into {INDEX_PATH}")

    print(f"\nSummary: {len(by_patient)} patients, {total_retained} findings retained "
          f"(of {len(reports)} reports)")


if __name__ == "__main__":
    main()
