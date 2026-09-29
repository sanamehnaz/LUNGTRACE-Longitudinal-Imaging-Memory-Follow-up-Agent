import json
import os
import sys
from collections import defaultdict
from datetime import datetime

INDEX_PATH = os.path.join(os.path.dirname(__file__), "data", "findings_index.json")


def check_open_loops(findings_index: dict) -> list:
    """Flag each patient's latest finding per organ/location whose follow-up is overdue with no later scan."""
    today = datetime.now().date()
    flags = []
    for patient_id, findings in findings_index.items():
        groups = defaultdict(list)
        for f in findings:
            groups[(f["organ"].lower(), f["location"].lower())].append(f)

        for items in groups.values():
            items.sort(key=lambda f: f["date"])
            latest = items[-1]
            due = latest.get("follow_up_due_date")
            if not due:
                continue
            due_date = datetime.strptime(due, "%Y-%m-%d").date()
            has_later_scan = any(f["date"] > due for f in items)
            if due_date < today and not has_later_scan:
                flags.append({
                    "patient_id": patient_id,
                    "patient_name": latest.get("patient_name", patient_id),
                    "organ": latest["organ"],
                    "location": latest["location"],
                    "finding_type": latest.get("finding_type", "finding"),
                    "follow_up_due_date": due,
                    "days_overdue": (today - due_date).days,
                })
    return flags


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else INDEX_PATH
    try:
        with open(path, encoding="utf-8") as f:
            index = json.load(f)
    except FileNotFoundError:
        sys.exit(f"{path} not found. Run ingest_to_hindsight.py first (or pass an index path).")

    flags = check_open_loops(index)
    flagged = {fl["patient_id"] for fl in flags}
    print(f"Checked on {datetime.now().date()}\n")
    for fl in flags:
        print(
            f"OPEN LOOP: {fl['patient_name']} ({fl['patient_id']}) - {fl['location']} "
            f"{fl['finding_type'].lower()} follow-up was due {fl['follow_up_due_date']}, "
            f"now {fl['days_overdue']} days overdue. No follow-up scan found."
        )
    for pid, findings in index.items():
        if pid not in flagged:
            name = findings[0].get("patient_name", pid)
            print(f"No open loops for {name} ({pid})")
