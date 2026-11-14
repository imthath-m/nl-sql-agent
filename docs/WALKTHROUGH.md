# Walkthrough

A plain-language tour of nl-sql-agent. It is for a new teammate or an interviewer. No live results are quoted here. Figures live in `results/summary.md` and `results/tune/report.md` once those files are generated (see the README status table).

Commands assume the setup in the README: venv active, `export PYTHONPATH=src`, run from the repo root.

## 1. What problem this solves

People ask data questions in English. Databases need SQL. This agent turns a question into a SQLite query, runs it, and explains the answer in one sentence.

The hard part is not writing SQL. It is knowing when the answer is wrong. So most of this repo is an eval suite: tools that measure, explain and audit the agent's mistakes.

The test bed is BIRD, a public benchmark. It has real, messy databases, a question for each, an "evidence" hint (outside knowledge the question needs), and a gold SQL answer.

## 2. The pipeline, step by step

Question in, answer out. Each step below says what it does, why it exists, and where the code is. The whole flow is `loop.solve` in `src/nlsql/loop.py`. It is plain Python, a fixed sequence, not an agent that picks its own tools.

1. **Question.** The input is a question, the BIRD evidence hint, and a database id.
   - Why: the evidence hint is part of what BIRD tests.
   - File: `src/nlsql/bird.py` (loads examples, schemas, fixed-seed subsets).

2. **Retrieve tables.** Embed each table (name plus columns) and the question. Keep the top 5 tables by cosine similarity.
   - Why: big schemas distract the model. Fewer tables, fewer wrong joins.
   - Small schemas (5 tables or fewer) pass through untouched.
   - File: `src/nlsql/retriever.py`.

3. **Plan.** One model call writes 2 to 6 plain-language steps. No SQL.
   - Why: breaking a question into steps helps with multi-part questions.
   - File: `src/nlsql/planner.py`.

4. **Generate SQL.** One model call writes a single SQLite query from the pruned schema, the evidence and the plan. The prompt is a versioned file.
   - Why: this is the core translation.
   - Files: `src/nlsql/agent.py`, `src/nlsql/prompts/generator_v1.txt`.

5. **Execute.** Run the SQL on the real database. Capture rows or the error text. It never raises.
   - Why: the database is the cheapest judge of whether SQL is valid.
   - File: `src/nlsql/executor.py`.

6. **Repair.** If execution failed, send the SQL and the error back to the model. Run again. Up to 3 repairs.
   - Why: many first-try failures are small (a wrong column name). The error message tells the model what to fix.
   - Files: `src/nlsql/agent.py` (`repair_sql`), `src/nlsql/loop.py`.

7. **Explain.** If the SQL ran, one model call restates the result rows as one plain sentence.
   - Why: a non-technical reader can check the answer without reading SQL.
   - File: `src/nlsql/explainer.py`.

8. **Confidence gate (abstain).** Score confidence from signals already on hand. No extra model call. Refuse to answer below a threshold and route the case to a human.
   - Three signals: how well the retrieved tables matched the question, whether the SQL used the retrieved tables, and whether it ran cleanly or needed repairs.
   - Why: a wrong answer delivered confidently is worse than "I'm not sure, ask a person". BIRD does not reward abstaining. This gate is our own addition, so we measure it separately.
   - Files: `src/nlsql/gate.py`, `src/nlsql/gate_eval.py`.

9. **Trace.** Every step above is recorded with a short summary, timing and timestamp. The trace links the answer to its SQL and the SQL to the schema it was written against.
   - Why: an auditor can follow any answer back to its evidence.
   - File: `src/nlsql/trace.py`. A static HTML page renders traces, lowest confidence first: `src/nlsql/review.py`.

## 3. The eval suite

All of this runs on a saved run file (JSON), offline, except the LLM judge.

- **Execution accuracy (EX).** Run the predicted SQL and the gold SQL. Count it correct if both return the same set of rows. Errors and timeouts count as wrong.
  - Why set equality: many different SQL texts give the same answer. Comparing text would punish correct queries. Comparing result sets matches the official BIRD metric.
  - Limit: set equality ignores row order and duplicates, so it can pass a query that is subtly wrong. It can also fail a query that answers the question a different valid way.
  - File: `src/nlsql/score.py`.
- **Failure taxonomy.** Each wrong answer gets one rule-based cause: execution error, dialect error, schema linking, wrong aggregation, join error, wrong filter, wrong projection, or other. Rules compare predicted and gold SQL. They are heuristics, so some tags will be wrong.
  - Why: a single accuracy number does not say what to fix. A histogram of causes does.
  - File: `src/nlsql/taxonomy.py`.
- **Schema-linking metrics.** Which tables did the SQL use, versus the gold SQL? Precision, recall and F1. Also: did the retriever keep the tables the gold query needs?
  - Why: if the right table never reaches the model, nothing downstream can fix it.
  - File: `src/nlsql/schema_link.py`.
- **LLM judge.** A stronger model reads the question, gold SQL and predicted SQL and says whether the prediction answered the right question.
  - Why it can disagree with EX: the judge reads intent, EX compares rows. A query can return different rows yet answer the question (judge says right, EX says wrong). A query can match rows by luck while asking the wrong thing (EX says right, judge says wrong). The judge is also a model and can be wrong.
  - Files: `src/nlsql/judge.py`, and `python -m nlsql.judge_check`, which runs it on held-out rows and writes a confusion matrix, agreement, kappa and disagreement examples to `results/judge/report.md`.
- **Cost and latency.** Tokens per example turn into a dollar estimate from a price table (overridable with `NLSQL_PRICES`). Latency is reported as mean, p50 and p95. Thinking tokens are counted.
  - File: `src/nlsql/cost.py`.
- **Regression diff.** Compare two run files example by example. List examples that went from right to wrong, and the reverse. Exit non-zero on regressions, so it can gate CI.
  - Why: an average can stay flat while individual cases swap.
  - File: `src/nlsql/regression.py`.
- **Gate eval.** Two numbers: false-refusal rate (correct answers the gate refused, its cost) and caught-error rate (wrong answers it refused, its value). It sweeps the threshold offline from recorded confidences.
  - File: `src/nlsql/gate_eval.py`. Held-out split: `bird.gate_split`.
- **Review page.** One self-contained HTML file. Each example expands to show steps, SQL, retrieved schema, confidence and answer.
  - File: `src/nlsql/review.py`.

`src/nlsql/run_eval.py` runs taxonomy, schema-linking and cost over one run file. `src/nlsql/run_all.py` (via `scripts/run_all.sh`) runs baseline, loop and phase 3 on the same fixed-seed subset, so their scores compare like for like. It writes `results/summary.md`.

## 4. The prompt-tuning experiment

`python -m nlsql.tune` tests whether changing the generator prompt helps.

How it works:

1. Draw two disjoint fixed-seed splits: dev and held-out.
2. Run the baseline prompt (`generator_v1`) on dev.
3. Tag the failures with the taxonomy. Find the top causes.
4. Pick 2 or 3 prompt variants from `src/nlsql/prompts/` that target those causes. A manifest maps each variant to the cause it targets.
5. Pick the winner on dev only. Write it to `winner.json`.
6. Only then run baseline and winner on held-out.

Why dev and held-out: choosing a prompt on the same examples you report on overstates the gain. The prompt gets fitted to those examples. Held-out examples were never used to choose, so the result is fair.

Why freeze the winner first: if you can pick after seeing held-out numbers, you can pick whatever looks best. Writing `winner.json` before any held-out call removes that option. The file is the evidence.

What McNemar tells you: both prompts ran on the same examples. McNemar's exact test looks only at the examples where they disagree (one right, one wrong). It asks whether the split of those is lopsided more than chance. A paired bootstrap gives a confidence interval for the EX difference.

What it does not prove:

- It does not show the gain holds on other databases, models or benchmarks.
- A small held-out set gives a wide interval. "Not significant" is not "no effect".
- Model output can vary between runs, and this is one run.
- It tests prompt wording only. It says nothing about the retriever, planner or gate.

Outputs: `results/tune/report.md` and `results/tune/report.json`.

## 5. Limitations

- No live result is committed yet. Every number must come from `results/`.
- BIRD dev is public. The model may have seen it in training. Treat scores as checkpoints, not guarantees.
- EX compares result sets. It can mark right answers wrong and wrong answers right.
- The failure taxonomy is rule-based. It is a guide, not ground truth.
- The gate's confidence is a heuristic. It is not a calibrated probability.
- The planner decomposes in one up-front call. It does not choose tools or branch.
- The default embedding model `text-embedding-004` may be retired. If so, set `NLSQL_EMBED_MODEL`. This is unverified.
- SQLite only. Spider 2.0-Lite (BigQuery, Snowflake, huge schemas) is planned in `plan.md` and not started.
- The judge is an LLM, so it has its own errors.

## 6. Five-minute demo script

Pick one path. Path A needs a Gemini key. Path B needs nothing.

### Path A: with `GEMINI_API_KEY`

Run the long step before the call, or start it early in a second terminal. The default `--n 150` takes a while. For a live demo use a small `--n`.

| Time | Command | Say |
|------|---------|-----|
| 0:00-0:30 | `sed -n 1,12p README.md` | "This turns English questions into SQL on the BIRD benchmark. Most of the work is measuring when it is wrong." |
| 0:30-1:00 | `scripts/selfcheck.sh` | "Every component has an offline test. No key or data needed. This is the safety net." |
| 1:00-2:00 | `scripts/run_all.sh --n 20 --workers 4` | "One command. It fetches data, runs baseline, loop and the full pipeline on the same examples, then scores each. Same examples, so the comparison is fair." |
| 2:00-2:45 | `cat results/summary.md` | "Accuracy, change versus baseline, cost and latency per mode. With a sample this small, differences are noisy. I would not read much into them." |
| 2:45-3:30 | `python -m nlsql.review results/run_all/phase3.json` then open the HTML it prints | "Lowest confidence first. Open one row: steps, SQL, retrieved tables, answer. This is how an auditor follows an answer back." |
| 3:30-4:15 | `python -m nlsql.tune --dev 20 --heldout 40` then `cat results/tune/report.md` | "Pick a prompt on dev only. The winner is written to `winner.json` before held-out runs. Then compare on held-out with McNemar. Sizes here are small, so the interval will be wide." |
| 4:15-5:00 | `python -m nlsql.judge_check` then `cat results/judge/report.md` | "A second opinion from a stronger model. Where it disagrees with EX is where the metric is weakest. Here are example disagreements." |

Note on `review`: the page sorts by gate confidence. Only phase-5-style rows carry it. If rows in `phase3.json` have none, they sort in file order and the confidence panel is empty. To show the gate, run `python -m nlsql.run_phase5 --n 20` and pass the `runs/phase5_<ts>.json` it writes.

### Path B: offline fallback (no key)

| Time | Command | Say |
|------|---------|-----|
| 0:00-0:30 | `sed -n 1,12p README.md` | Same opening as Path A. |
| 0:30-1:30 | `scripts/selfcheck.sh` | "All offline tests pass without a key. Scorer, executor, retriever, taxonomy, gate, trace, review." |
| 1:30-2:30 | `sed -n 1,40p docs/WALKTHROUGH.md` | Walk the pipeline steps in section 2 and say what each one is for. |
| 2:30-3:00 | `python -m nlsql.review _selfcheck` | "The review page renders from a run file. This check proves the page sorts low confidence first and shows trace, SQL, schema and answer." |
| 3:00-4:15 | Build a toy run file and render it (below) | "These two rows are made-up demo data, not results. The low-confidence abstained row comes first." |
| 4:15-5:00 | Open `results/summary.md` if it exists, else `plan.md` | "No live run yet. Here is where the numbers will appear, and the roadmap." |

Toy run file for the offline demo (hand-written, not real output):

```bash
cat > /tmp/toy_run.json <<'JSON'
{"summary": {"abstain_threshold": 0.55},
 "results": [
  {"question_id": 1, "db_id": "shop", "question": "How many stores?",
   "confidence": 0.92, "abstain": false, "pred": "SELECT COUNT(*) FROM store",
   "gate_signals": {"retrieval": 0.9, "linking": 1.0, "execution": 1.0},
   "explanation": "There are 12 stores.", "retrieved_tables": ["store"]},
  {"question_id": 2, "db_id": "shop", "question": "An ambiguous question?",
   "confidence": 0.31, "abstain": true, "pred": "SELECT x FROM mystery",
   "gate_signals": {"retrieval": 0.2, "linking": 0.0, "execution": 1.0},
   "gate_reason": "confidence 0.31 < 0.55 (weakest signal: linking=0.00)",
   "explanation": null, "retrieved_tables": ["a", "b"]}]}
JSON
python -m nlsql.review /tmp/toy_run.json -o /tmp/toy_review.html
```

Then open `/tmp/toy_review.html` in a browser.
