# Demo: the research and evaluation workspace

A 15-minute walk-through for a business audience: how a research answer is
produced, and how we know it's any good (#93, #117). It runs on a laptop
against the local API. Every step has a fallback that needs no live agent
call, so a slow call or the subscription's session limit doesn't stall the
meeting.

## Setup (once, before the meeting)

```bash
docker compose -f docker/compose.yaml up -d postgres   # run store and filing-text search
export DATABASE_URL=postgresql://recon:<POSTGRES_PASSWORD>@localhost:55432/recon
unset ANTHROPIC_API_KEY                                 # the API refuses to start with it (ADR 0027)
uv run uvicorn recon.api.main:app --port 8000
```

Open http://localhost:8000. Check three things:

- The badge top right says **SEC EDGAR snapshot**. "Synthetic fixtures" means the EDGAR cache is missing.
- The mode picker offers **single** and **multi**.
- **Reopen a run** lists the demo runs. "Run history is off" means `DATABASE_URL` isn't set for the API.

### Record the demo runs (about €1, once)

Ask the three example questions from the page in single mode, and one of
them in multi mode. Each answered run is saved in Postgres and shows up
under **Reopen a run**. Back each one up, so a lost database doesn't lose
the demo:

```bash
mkdir -p data/demo-runs
curl -s localhost:8000/runs | python3 -c "import json,sys; [print(r['run_id']) for r in json.load(sys.stdin)['runs']]" |
  while read id; do curl -s -o "data/demo-runs/$id.json" "localhost:8000/runs/$id/export"; done
```

`data/` is gitignored, so these stay local. The export holds the question,
answer, claims, evidence and trace, and no keys or settings (#115).

Rate the runs you want to show 👍 now. The list then marks them.

## The story

### 1. Research: an answer you can check (5 min)

1. Point at the badge: the tools query a pinned snapshot of real SEC EDGAR filings, not the open web.
2. Under **Reopen a run**, open the Workday retention-metric run. It opens instantly, and the green banner says it's a saved run, not a live call.
3. Walk through the answer, then **Claims and sources**. Each claim lists the rows it rests on. Open a **verified** source: the company, form, filing date, section and the passage itself, with the accession number. The server checked that a tool really returned that row in this run. The model's own word isn't trusted.
4. If a source is **unverified**, say so plainly: the model cited something no tool returned, and the workspace shows it rather than hiding it.
5. Scroll to **Research trace**: every tool call, its arguments, status and time. Then the summary: mode, time, cost, tokens.

### 2. Live question (optional, 2–5 min)

Click an example chip, keep **single** mode, and run it. Single mode takes
1–2 minutes. Multi mode can take up to 5, so don't run multi live.

Fallback: if it's slow or hits the session limit, go back to step 1's saved
runs. Nothing in the rest of the story needs a live call.

### 3. Evaluation: how we know it's good (5 min)

1. Switch to **Evaluation** (or open http://localhost:8000/#evaluation). The label says it: benchmark results, not live answers.
2. **Recorded runs**: every run scored the agent on the same fixed benchmark questions. Point at task completion, answer score, cost, and cost per correct answer.
3. **Compare** opens on multi mode with routing off against routing on: the local model plans the work, Claude does the rest. Read the verdicts. "same" means the change is within the measured run-to-run noise (ADR 0028), so we don't call noise an improvement.
4. Pick an older rubric-3 run as the baseline and compare again. The gate refuses and lists why: the scoring changed, so the numbers aren't evidence of better or worse.
5. Click a run id in the table to show its per-case results, including the cases that didn't complete and why.

### 4. Feedback closes the loop (1 min)

Back on a saved run, click 👍 or 👎 and add a note. These labels are the
start of the set we'll use to check the LLM judge against people.

## Fallbacks

| What goes wrong | Do this |
|---|---|
| A live call is slow or hits the session limit | Reopen a saved run (step 1). |
| "Run history is off" | Restart the API with `DATABASE_URL` exported. |
| Postgres lost the runs | Show the JSON in `data/demo-runs/` in an editor; the Evaluation tab still works. |
| The API won't start: API key exported | `unset ANTHROPIC_API_KEY` and start it again. |
| The badge says "Synthetic fixtures" | The EDGAR cache is missing on this machine (`docs/data-sources.md`). Show saved runs only. |
| Evaluation shows no runs | Start the API from the repo root: it reads `evals/results/` (`RECON_EVAL_RESULTS_DIR`). |
