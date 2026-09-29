# LungTrace

**LungTrace: an agent that remembers a patient's scans over time, using Hindsight.**

## The Problem

A radiologist reading today's scan does not automatically see how a finding has changed since earlier scans. A 4.5 mm nodule looks unremarkable on its own, but not if it measured 3 mm four months ago. Follow-up recommendations ("repeat CT in 6 months") are also easy to lose: they live in the text of a report, and nothing reliably checks that the follow-up scan actually happened. Missed follow-up and lack of comparison with prior imaging are well-documented patient-safety issues, and they can delay the diagnosis of conditions such as lung cancer.

## What It Does

1. **Extracts structured findings** (organ, location, finding type, size, recommendation, follow-up due date) from free-text radiology reports using an LLM (Groq).
2. **Retains each finding** into a per-patient Hindsight memory bank.
3. **Recalls a patient's history** and generates an interval-change summary, for example: `Right upper lobe nodule: 3mm (2026-01-10) -> 4.5mm (2026-05-12) -> 6mm (2026-09-08). Growth: 100% over 8 months.`
4. **Flags open loops**: recommended follow-ups whose due date has passed with no later scan on record.
5. **Provides an Overview screen** listing every patient with an overdue follow-up, sorted worst-first by days overdue, so a clinician can see what needs attention at a glance.
6. **Generates an on-demand Clinical Summary** using Hindsight's `reflect`, synthesizing a patient's findings, trend, and follow-up concerns into a short narrative.
7. **Processes new reports live** through a "Submit New Report" panel: the report is extracted, saved to memory, and the dashboard updates immediately.

## Where Hindsight Fits

Each patient has their own Hindsight memory bank (`bank_id` is the patient ID). Every extracted finding is written with `retain`, timestamped with the report date, and `recall` reads the patient's history back when a summary is built. Memory is the core of the product rather than a bolt-on: the value comes from comparing today's finding against what was seen before, which a single-report LLM call cannot do. The dashboard's "Memory: ON / OFF" switch demonstrates this directly.

Implementation notes: Hindsight's `retain` runs its own fact extraction, so recalled text can be rephrased. For exact sizes and dates, the interval and open-loop logic reads a small local index (`data/findings_index.json`) written at ingestion time, while `recall` still retrieves the timeline from Hindsight. `reflect` powers the on-demand Clinical Summary, which complements the deterministic interval-change calculation with LLM-generated narrative context. Re-running ingestion is safe: it merges into the local index rather than overwriting it, so reports submitted live are kept.

## Tech Stack

- Python
- FastAPI
- Groq (`openai/gpt-oss-120b`)
- Hindsight Cloud
- Vanilla HTML/CSS/JS frontend (single page, no build step)

## Setup

1. Clone the repository and enter it:
   ```bash
   git clone https://github.com/sanamehnaz/LUNGTRACE-Longitudinal-Imaging-Memory-Follow-up-Agent.git
   cd LUNGTRACE-Longitudinal-Imaging-Memory-Follow-up-Agent
   ```
2. Create a `.env` file in the project root:
   ```
   HINDSIGHT_API_KEY=your_hindsight_key
   HINDSIGHT_BASE_URL=https://api.hindsight.vectorize.io
   GROQ_API_KEY=your_groq_key
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Seed the data (extracts each synthetic report, retains it into Hindsight, and writes `data/findings_index.json`):
   ```bash
   python ingest_to_hindsight.py
   ```
5. Start the app:
   ```bash
   uvicorn app:app --reload
   ```
6. Open http://localhost:8000.

## Project Structure

```
.
├── app.py                      # FastAPI app: dashboard, patient API, /api/process-report
├── extraction.py               # LLM extraction of structured findings from report text (Groq)
├── ingest_to_hindsight.py      # Seeds Hindsight banks and the local findings index
├── interval_check.py           # Interval-change summaries (growth/shrinkage over time)
├── open_loop_check.py          # Detects overdue follow-ups with no later scan
├── templates/
│   └── dashboard.html          # Single-page dashboard (HTML/CSS/JS)
├── data/
│   └── synthetic_reports.json  # Synthetic CT reports for 6 fictional patients
└── requirements.txt            # Python dependencies
```

## Important Disclaimer

This project uses **100% synthetic patient data**. It is a decision-support prototype, **not a diagnostic tool**, and is not intended for clinical use without proper validation and regulatory review.

## Demo Flow

1. Sign in with any name and clinic (the login is cosmetic).
2. You land on the **Overview** screen, which lists every patient with an overdue follow-up, worst first. Click a card to jump straight to that patient.
3. Select **Rajesh Kumar (P001)** under Patient overview. The interval summary shows a nodule growing 3 mm -> 4.5 mm -> 6 mm, which is alarming across time but unremarkable in any single report.
4. Toggle **Memory OFF**. The view collapses to the latest report alone, with no trend and no warning. Toggle it back ON.
5. Select **Sunita Reddy (P002)**. A red open-loop alert shows a follow-up that was due 2026-07-15 and never happened.
6. In **Submit New Report**, choose P002, set a date, paste a new report, and click **Process Report**. The finding is extracted, saved to memory, and the open-loop alert clears as the dashboard updates.
7. With Memory ON, click **Generate Clinical Summary** for a patient to show Hindsight's `reflect` synthesizing their history into a short narrative.
