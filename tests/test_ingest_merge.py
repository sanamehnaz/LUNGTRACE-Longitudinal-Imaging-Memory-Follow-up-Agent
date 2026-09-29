# Run from the repo root: python tests/test_ingest_merge.py  (mocks Groq and Hindsight; no network needed)
import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json, os, tempfile, copy
from unittest.mock import MagicMock, patch
import ingest_to_hindsight as ing

reports = json.load(open("data/synthetic_reports.json"))
truth = {(r["patient_id"], r["date"]): r for r in reports}
def fake_extract(text, pid, date):
    r = truth[(pid, date)]
    return {k: r[k] for k in ("patient_id","date","organ","location","finding_type","size_mm","recommendation","follow_up_due_date")}

tmp = tempfile.mkdtemp(); idx_path = os.path.join(tmp, "findings_index.json")
# Pre-existing index containing a LIVE-submitted report (not in synthetic_reports.json) + a stale copy of one seeded report
live = {"patient_name":"Sunita Reddy","date":"2026-09-29","organ":"Lung","location":"Left lower lobe","finding_type":"Nodule","size_mm":5,"recommendation":"No follow-up needed","follow_up_due_date":None}
stale = {"patient_name":"Rajesh Kumar","date":"2026-01-10","organ":"Lung","location":"Right upper lobe","finding_type":"Nodule","size_mm":99,"recommendation":"stale","follow_up_due_date":None}
json.dump({"P002":[live],"P001":[stale]}, open(idx_path,"w"))

hs = MagicMock()
def run():
    with patch.object(ing,"INDEX_PATH",idx_path), patch.object(ing,"Hindsight",return_value=hs), patch.object(ing,"extract_finding",side_effect=fake_extract):
        ing.main()
    return json.load(open(idx_path))

import io, contextlib
with contextlib.redirect_stdout(io.StringIO()): first = run()
with contextlib.redirect_stdout(io.StringIO()): second = run()

count = lambda i: sum(len(v) for v in i.values())
print("after run 1:", {k:len(v) for k,v in sorted(first.items())}, "total", count(first))
print("after run 2:", {k:len(v) for k,v in sorted(second.items())}, "total", count(second))
print("run1 == run2 (idempotent):", first == second)
print("expected total =", len(reports)+1, "(13 seeded + 1 live)")
assert count(second) == len(reports)+1
assert any(f["date"]=="2026-09-29" for f in second["P002"]), "live finding lost"
p1 = [f for f in second["P001"] if f["date"]=="2026-01-10"]
assert len(p1)==1 and p1[0]["size_mm"]==3, p1
for pid, fs in second.items():
    keys=[(f["date"],f["organ"].lower(),f["location"].lower()) for f in fs]; assert len(keys)==len(set(keys)), pid
    assert [f["date"] for f in fs]==sorted(f["date"] for f in fs)
print("live finding kept: True | stale P001 entry refreshed to 3mm: True | no duplicate keys, sorted by date: True")
print("hindsight retain calls:", hs.retain.call_count, "(2 runs x 13 reports)")
print("tmp file left behind:", os.path.exists(idx_path+".tmp"))

# failure case: extraction fails for everything -> existing index must survive
def boom(*a): raise RuntimeError("groq down")
before=json.load(open(idx_path))
with patch.object(ing,"INDEX_PATH",idx_path), patch.object(ing,"Hindsight",return_value=hs), patch.object(ing,"extract_finding",side_effect=boom), contextlib.redirect_stdout(io.StringIO()):
    ing.main()
print("index intact after total extraction failure:", json.load(open(idx_path))==before)
