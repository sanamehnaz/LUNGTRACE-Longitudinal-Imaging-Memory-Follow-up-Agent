import json
import os
import re

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

MODEL = "openai/gpt-oss-120b"
EXTRACTED_FIELDS = [
    "organ",
    "location",
    "finding_type",
    "size_mm",
    "recommendation",
    "follow_up_due_date",
]
FIELDS = ["patient_id", "date"] + EXTRACTED_FIELDS

SYSTEM_PROMPT = """You extract structured data from radiology reports.
Return ONLY a valid JSON object with exactly these keys and nothing else:
- organ (string)
- location (string)
- finding_type (string, e.g. "Nodule", "Lesion")
- size_mm (number)
- recommendation (short text)
- follow_up_due_date (YYYY-MM-DD: the report date plus the interval in the recommendation, e.g. "in 6 months" means the same day 6 months later; null if no follow-up is needed)
The user message gives the patient ID and report date as known context; use the report date to compute follow_up_due_date. Do not return patient_id or date.
No markdown, no code fences, no commentary. Use null for anything not stated in the report; do not guess."""

_client = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


def parse_json_response(raw: str) -> dict:
    """Parse model output as JSON, tolerating markdown code fences and surrounding text."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def extract_finding(report_text: str, patient_id: str, date: str) -> dict:
    response = _get_client().chat.completions.create(
        model=MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"This report is for patient {patient_id}, dated {date}.\n\n{report_text}",
            },
        ],
    )
    data = parse_json_response(response.choices[0].message.content)
    result = {"patient_id": patient_id, "date": date}
    result.update({field: data.get(field) for field in EXTRACTED_FIELDS})
    return result


def fields_match(field: str, extracted, truth) -> bool:
    """Compare one field. Strings are case-insensitive; location allows containment either way."""
    if field in ("organ", "location", "finding_type") and isinstance(extracted, str) and isinstance(truth, str):
        a, b = extracted.strip().lower(), truth.strip().lower()
        if field == "location":
            return bool(a) and bool(b) and (a in b or b in a)
        return a == b
    return extracted == truth


if __name__ == "__main__":
    path = os.path.join(os.path.dirname(__file__), "data", "synthetic_reports.json")
    with open(path, encoding="utf-8") as f:
        reports = json.load(f)

    matches = total = 0
    for i, report in enumerate(reports, 1):
        truth = {field: report[field] for field in FIELDS}
        try:
            extracted = extract_finding(report["report_text"], report["patient_id"], report["date"])
        except Exception as e:
            print(f"=== Report {i}: {report['patient_id']} {report['date']} === ERROR: {e}\n")
            continue
        print(f"=== Report {i}: {report['patient_id']} {report['date']} ===")
        print("EXTRACTED:   ", json.dumps(extracted))
        print("GROUND TRUTH:", json.dumps(truth))
        for field in FIELDS:
            if field == "recommendation":
                continue  # free text; compare visually
            total += 1
            ok = fields_match(field, extracted[field], truth[field])
            matches += ok
            if not ok:
                print(f"  MISMATCH {field}: extracted={extracted[field]!r} truth={truth[field]!r}")
        print()
    print(f"Matching fields (excluding recommendation): {matches}/{total}")
