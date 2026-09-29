# The Patient-Safety Bug Nobody Talks About: What I Learned Building Memory Into a Radiology Agent

A 3 mm lung nodule is unremarkable. So is a 4.5 mm one, and so is a 6 mm one, as long as you only ever read one report at a time. Put all three in a row and you have a patient whose nodule doubled in eight months. I built LungTrace around that gap.

## The problem is the seam between reports

A radiologist reading today's CT gets a report to write, not a longitudinal record to study. The prior scans exist, but seeing how a finding has changed depends on someone opening them and comparing. The second failure is quieter. A report says "follow-up CT in 6 months," and then nothing checks whether that scan ever happened. The recommendation lives in free text, in a document nobody is going to re-read.

Neither problem is a failure of interpretation. Both are failures of memory. That framing shaped the whole design.

## What LungTrace does

LungTrace ingests free-text radiology reports, extracts a structured finding from each one, and keeps a per-patient memory of everything it has seen. From that memory it produces three things a clinician can act on:

- **An interval-change summary**, e.g. `Right upper lobe nodule: 3mm (2026-01-10) -> 4.5mm (2026-05-12) -> 6mm (2026-09-08). Growth: 100% over 8 months.`
- **Open-loop flags** for follow-ups that came due and never got a scan.
- **An overview screen** that lists every overdue patient, worst first, so the first thing a doctor sees after signing in is what needs attention.

Under the hood it's a small Python service. FastAPI serves a single-page dashboard. Groq (`openai/gpt-oss-120b`) does extraction. Each patient gets their own memory bank in [Hindsight](https://github.com/vectorize-io/hindsight), an open-source agent memory system, with the bank ID set to the patient ID. A new report can be submitted live: it gets extracted, retained, indexed, and the dashboard updates without a reload.

## The through-line: three jobs, three mechanisms

The most important design decision was refusing to let one component do everything. It's tempting to hand a model the whole patient history and ask "what changed?" I split the work three ways instead:

1. **The LLM reads.** It turns a messy paragraph into structured fields. That's all.
2. **Deterministic code computes.** Percent growth, elapsed months, and "is this follow-up overdue" are arithmetic. They don't get to be probabilistic.
3. **Memory remembers, and narrates when asked.** Hindsight holds the patient's history and retrieves it, and its `reflect` call synthesizes a narrative when a clinician wants one.

Each job has a different failure mode, and keeping them separate means each can be tested and trusted on its own terms. I'll go through them in order.

### Job one: extraction, with the date given, not guessed

The extractor takes the report text plus the patient ID and date as known context. The report date is not in the model's job description:

```python
def extract_finding(report_text: str, patient_id: str, date: str) -> dict:
    response = _get_client().chat.completions.create(
        model=MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": f"This report is for patient {patient_id}, dated {date}.\n\n{report_text}"},
        ],
    )
    data = parse_json_response(response.choices[0].message.content)
    result = {"patient_id": patient_id, "date": date}
    result.update({field: data.get(field) for field in EXTRACTED_FIELDS})
    return result
```

Two small choices matter here. `patient_id` and `date` are written into the result by the caller, so the model can't alter identity fields. And the prompt tells the model to return null rather than guess. The first version tried to extract everything from the text, including patient ID and date, and the report body doesn't contain them. Making the model infer or invent identifiers is exactly the wrong place to be creative.

The model does compute `follow_up_due_date` from "in 6 months," and I'd flag that as the least deterministic step in the pipeline. Month arithmetic is where LLMs slip, and my comparison harness checks it against ground truth for that reason.

### Job two: the arithmetic lives in plain Python

Once findings are structured, everything downstream is ordinary code. The interval summary:

```python
def summarize_findings(findings: list) -> str:
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
        chain = " -> ".join(f"{_fmt(f['size_mm'])}mm ({f['date']})" for f in items)
        change = (last["size_mm"] - first["size_mm"]) / first["size_mm"] * 100
        ...
```

Findings are grouped by organ and location, so a nodule in the right upper lobe is never compared against one in the left lower lobe. And the open-loop check is a dozen lines:

```python
latest = items[-1]
due = latest.get("follow_up_due_date")
if not due:
    continue
due_date = datetime.strptime(due, "%Y-%m-%d").date()
has_later_scan = any(f["date"] > due for f in items)
if due_date < today and not has_later_scan:
    flags.append({..., "days_overdue": (today - due_date).days})
```

Nothing in here can hallucinate. If the dashboard says a follow-up is 76 days overdue, that number came from subtraction. For a tool that a clinician might act on, I wanted the numbers to be boring.

### Job three: what Hindsight is actually for

Every extracted finding is written to that patient's bank as a natural-language sentence, timestamped with the report date:

```python
client.retain(
    bank_id=patient_id,
    content=build_content(finding),
    timestamp=datetime.strptime(report["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc),
    document_id=f"{patient_id}-{report['date']}",
)
```

The `timestamp` matters: it tells the memory when the event happened, not when I ingested it, so the timeline is the clinical one. The `document_id` matters more than it looks, and I'll come back to it.

This is where I had to be honest about what a memory system is for. [Agent memory](https://vectorize.io/what-is-agent-memory) is not a database with better marketing. When you retain text, the system extracts and organizes facts from it, so what recall hands back is a rephrasing, not my exact string. That is a feature when you want retrieval by meaning, and a liability when you need exact numbers. If I'd tried to reconstruct "3mm to 6mm" by regex-parsing recalled text, I'd have been building on sand.

So the exact sizes and dates live in a small structured index (`findings_index.json`), and Hindsight does what it's good at: holding the patient's history and answering open-ended questions about it. The interval summary calls `recall` on the patient's bank to pull the timeline, and the structured index supplies the numbers. When a clinician wants the story rather than the arithmetic, a button calls `reflect`:

```python
SUMMARY_QUERY = (
    "Summarize this patient's imaging findings, the trend over time, "
    "and any follow-up concerns a clinician should know about."
)

response = client.reflect(bank_id=patient_id, query=SUMMARY_QUERY)
```

That endpoint deliberately caches nothing. It's an "ask the memory" feature, so every click goes to Hindsight, and the result is labeled in the UI as AI-generated from patient memory, to be verified against source reports. The [Hindsight docs](https://hindsight.vectorize.io/) cover retain, recall and reflect as three distinct operations, and that distinction turned out to map cleanly onto my three jobs: retain to write, recall to retrieve, reflect to synthesize.

## What it looks like in use

**P001 (growth).** Three reports over eight months, each recommending a follow-up in three to four months. Read individually, each says "small nodule, probably benign." With memory on, the Interval Change card renders the series as 3 mm, 4.5 mm, 6 mm with a trend line and a "Growth: 100% over 8 months" badge. Flip the Memory switch off and the view collapses to the latest report alone: 6 mm, follow-up in three months, no trend, no warning. I added that toggle because it proves the point better than any description: it shows exactly what the reader loses when the seam between reports isn't bridged.

**P002 (missed follow-up).** One report, a 5 mm left lower lobe nodule, follow-up due 2026-07-15. No later scan exists. The open-loop check flags it, the patient detail view shows a red alert, and the Overview screen ranks it by days overdue. Then I paste in a new report through the Submit New Report panel. It goes through extraction, gets retained into the patient's bank, gets merged into the index, and the alert clears, all without a page refresh. If that follow-up scan had also recommended another one, the loop would reopen with a new due date. The loop is a property of the data, not a flag someone has to remember to set.

## The bug that taught me the most: two sources of truth

My first ingestion script rebuilt `findings_index.json` from the seed data every time it ran. That was fine until live submissions existed. A report submitted through the API lived in Hindsight and in the index, but not in the source dataset, so re-running ingestion silently erased it from the index while Hindsight still remembered it. The dashboard and the memory disagreed, and nothing threw an error.

The fix was to make ingestion a merge instead of a rebuild, keyed on the finding's identity:

```python
def merge_finding(index: dict, patient_id: str, entry: dict) -> None:
    key = lambda f: (f["date"], f["organ"].lower(), f["location"].lower())
    findings = index.setdefault(patient_id, [])
    findings[:] = [f for f in findings if key(f) != key(entry)]
    findings.append(entry)
    findings.sort(key=lambda f: f["date"])
```

Both the batch path and the live-submit endpoint now call this same function, so they can't drift. Combined with the fixed `document_id` on every `retain` call, running ingestion twice produces identical results, and a test enforces it: it seeds an index with a live-submitted report and a stale entry, runs ingestion twice with the extractor and memory client mocked, and asserts no duplicates, no lost findings, and no change between runs. It also checks that a run where extraction fails for every report leaves the existing index untouched, which the old overwrite behavior would not have.

## Lessons

**Separate the math from the model.** If a number can be computed, compute it. Use the LLM where the input is genuinely unstructured, and keep it away from arithmetic, identity fields, and anything a clinician might act on directly.

**Treat memory as retrieval and synthesis, not storage of record.** Hindsight is the right home for "what do we know about this patient" and for narrative questions. It isn't where I keep the number I'm going to subtract from. A structured index next to it costs a few dozen lines and removes an entire class of parsing bugs.

**Give every write a stable identity.** The `document_id` pattern made retain idempotent. The merge key made the index idempotent. Together they turned "is it safe to re-run this?" from a worry into a test.

**Build the "memory off" switch.** It's the cheapest way to demonstrate that memory changes the outcome, and it doubles as a debugging tool: if the memory-off view and the memory-on view look the same, the memory isn't doing anything.

**Be explicit about what the AI produced.** The Clinical Summary is labeled as AI-generated, footnoted with a verify-against-source note, and separated visually from the computed numbers. A reader should always be able to tell which claims are arithmetic and which are synthesis.

## Where it goes from here

The pieces are deliberately small: an extractor, two pure-Python checks, a memory bank per patient, and a thin API. The next steps are the boring, important ones: real report ingestion from the imaging system, validation of extraction accuracy against radiologist-labeled data, and a review process before anything like this touches care decisions. It's decision support, not diagnosis, and it should stay that way.

The code is small enough to read in an afternoon. If you're building anything where an agent needs to remember something across time, the [Hindsight repo](https://github.com/vectorize-io/hindsight) is a good place to start. The thing I'd most want you to take from this project isn't a library choice, though. It's that the hard part of building agents with memory is deciding which of your system's jobs should never be left to the model.
