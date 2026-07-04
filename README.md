# nl-sql-agent

NL→SQL analytics agent. Phase 1: BIRD-SQL baseline. See [plan.md](plan.md) for the roadmap.

## Phase 1 — status: done

Single-pass Gemini Flash generator scored with the official BIRD execution-accuracy (EX) metric.

**Baseline: 66% EX** on the 50-example smoke subset (`gemini-2.5-flash`, 0 errors). Latest run in `runs/`.

## Phase 2 — status: done (built; awaiting live re-run)

Executor loop + Result-Explainer wrapped around the phase-1 generator:

- **Executor** (`executor.execute`) runs the generated SQL and captures the DB error string.
- On failure the error is fed back through **`agent.repair_sql`** and the query re-run, up to `--max-repairs` (default 3).
- Once the SQL runs, the **Result-Explainer** (`explainer.explain`) restates the answer in one plain sentence.
- `run_loop` re-scores the smoke subset and logs `ex_delta_vs_baseline` against the last baseline in `scores.log`.

Run `python -m nlsql.run_loop` (needs the BIRD data + a `GEMINI_API_KEY`) to record the score delta.

## Phase 3 — status: built (awaiting first full BIRD run)

Schema-Retriever + Planner bolted onto the front of the phase-2 loop, run over the **full BIRD dev set** (1534 examples):

- **Schema-Retriever** (`retriever.retrieve_schema`) indexes each table as `name + columns`, embeds it with `text-embedding-004`, and cosine-matches the question+evidence to keep only the **top-k** tables (default 5). Table doc embeddings are cached per DB; schemas with `<= top_k` tables pass through untouched (no embedding call). The pruned DDL — not the full schema — is what the planner and generator see.
- **Planner** (`planner.plan`) makes one Gemini call that decomposes the question into 2–6 ordered steps. Those steps are injected into the generator prompt (`agent.generate_sql(..., plan=...)`).
- `loop.solve(..., retrieve_top_k=5, use_planner=True)` runs the full pipeline: **retrieve → plan → generate → execute → repair → explain**. Both stages are opt-in — the defaults reproduce phase 2 exactly.
- `run_phase3` scores the full dev set and logs `mode: phase3` with `ex_delta_vs_baseline` / `ex_delta_vs_loop`.

Run `python -m nlsql.run_phase3` (needs the BIRD data + a `GEMINI_API_KEY`) to record the first full BIRD score.

## Phase 4 — status: built (eval suite; runs offline over run files)

The core deliverable: an eval suite that turns a run JSON (`runs/*.json` from any of the runners) into failure analysis, quality checks, and cost/latency — all offline except the opt-in LLM judge. The three runners now also record `latency_s` + token usage per example (`agent.reset_usage`/`get_usage`, backward-compatible — old run files still summarize).

- **Failure taxonomy** (`taxonomy.tag_failure`) tags each wrong example with one rule-based cause: `execution_error`, `dialect_error`, `schema_linking`, `wrong_aggregation`, `join_error`, `wrong_filter`, `wrong_projection`, or `other` (`correct` when EX=1). Priority-ordered heuristics compare predicted vs gold SQL + captured errors — no DB or API.
- **Schema-linking accuracy** (`schema_link`) extracts the tables a query touches (regex parser) and scores predicted-vs-gold table precision/recall/F1 + the phase-3 retriever's recall against the gold tables.
- **Cost + latency** (`cost.run_cost`) sums per-example tokens into a USD estimate from a per-model price table (override via `NLSQL_PRICES`) and reports latency mean/p50/p95.
- **LLM judge** (`judge.judge`) — a frontier model (`config.JUDGE_MODEL`, offline-only per plan) scores whether the prediction *answered the right question*, catching semantically-correct answers EX marks wrong. Opt-in (`run_eval --judge`).
- **Regression suite** (`regression`) diffs a new run file against a previous one per example, flags EX 1→0 regressions, and exits non-zero for CI / a pre-commit gate.
- `run_eval` ties taxonomy + schema-linking + cost (+ optional judge) over one run file and writes `runs/eval_<ts>.json`.

Everything ships a `_selfcheck` and needs no API/data. Run the judge / a full re-eval later on the user's machine.

## Phase 5 — status: built (abstention gate; eval offline over run files)

An **Abstention-Gate** that refuses low-confidence answers and routes them to a human instead of returning SQL. **This gate is our addition — BIRD does not reward abstention** (EX only scores answered examples); its value is operational, so we score it on its own held-out split with our own metrics.

- **Gate** (`gate.decide`) scores schema-linking confidence with a cheap, rule-based, offline signal — **no LLM call**. It combines three signals in [0,1]: `retrieval` (mean cosine of the tables the retriever kept), `linking` (fraction of the generated SQL's tables that were actually retrieved — agreement), and `execution` (1.0 clean, decaying per repair, 0.0 if it never ran). Overall confidence is their mean; the gate refuses below `config.ABSTAIN_THRESHOLD` (`NLSQL_ABSTAIN_THRESHOLD`, default 0.55). When retrieval is off, the retrieval/linking signals default to 1.0 (nothing to penalize), so baseline/loop rows never falsely abstain.
- The retriever gained a backward-compatible `retrieve_scored` that exposes the kept tables' cosine scores (the gate's main input); `retrieve` is now defined in terms of it and is unchanged.
- `loop.solve(..., use_gate=True, abstain_threshold=None)` runs the full phase-3 pipeline, then attaches `confidence` / `abstained` / `abstain_reason` / `gate_signals` to the `Solution`. Opt-in and backward-compatible — the defaults reproduce phases 2–4 exactly. A refused example keeps its SQL for audit but is marked `abstained` and returns no explanation (routed to a human).
- **Gate split** (`bird.gate_split`) is a deterministic 100-example sample drawn **disjoint from the smoke subset** (same seed pattern), so the gate is never tuned on the smoke set.
- **Gate eval** (`gate_eval`) turns a phase-5 run file into the gate's own metrics: **false-refusal rate** (of the answers that would have been correct, the share the gate refused — its cost), **caught-error rate** (of the wrong answers, the share it refused — its value), abstention rate, and served accuracy. It also **sweeps the threshold** offline from the recorded per-row confidences, so the operating point is tunable without re-running the pipeline.
- `run_phase5` runs the gate on its held-out split, logs every abstention, records confidence + decision per example, and writes `mode: phase5` with the gate metrics + threshold sweep.

Everything ships a `_selfcheck` and needs no API/data. Run `run_phase5` later on the user's machine for live gate numbers.

## Phase 6 — status: built (auditability; traces + review view, offline)

Every pipeline step is now recorded as a trace, and a thin static **review view** renders those traces low-confidence-first — so an auditor can open one HTML file and follow each answer back through its SQL to the schema it was written against.

- **Trace** (`trace.Trace`) records the pipeline's steps in order — retrieve → plan → generate → execute → (repair → execute)* → explain → gate — each with a name, a compact summary of inputs/outputs, latency, and a timestamp. It also stores the explicit audit link: **answer → final SQL → retrieved schema** (kept tables + the pruned DDL that SQL was written against). Everything is stored as short summaries (long strings capped), never full prompts, so a trace embeds inside the run file next to its result row.
- `loop.solve(..., trace=True)` builds one trace per example and attaches `Trace.to_dict()` to the `Solution.trace` field. Opt-in and backward-compatible — the default (`trace=False`) leaves `Solution.trace` `None` and reproduces phases 2–5 exactly. `run_phase5` now captures traces per example (`row["trace"]`).
- **Review view** (`review.render` → `runs/review_<ts>.html`) turns a run file into a single self-contained HTML page — no web framework, no new dependencies, inline CSS, only the native `<details>` toggle. Examples are sorted **ascending by gate confidence (lowest first)**, so refusals and shaky answers surface at the top; un-scored rows sort last. Each row expands to show the step trace, the final SQL, the retrieved tables + pruned schema, the confidence signals + abstention decision, and the plain-language answer. It consumes run/trace JSON offline and tolerates partial rows.

Everything ships a `_selfcheck` and needs no API/data. Generate a page from any run file with `python -m nlsql.review runs/phase5_<ts>.json`.

## Layout

- `bird-data/` and `bird-sql/` live in the **parent** dir (kept out of this repo — data is 700MB).
  - `bird-data/dev_20240627/` — BIRD dev set (1534 examples, 11 SQLite DBs, gold SQL).
  - `bird-sql/` — cloned BIRD-SQL repo (official evaluation scripts, reference).
- `src/nlsql/` — the agent.

## Component contracts

| Module | Contract |
|--------|----------|
| `bird.load_examples()` | → list of dev examples (`question`, `evidence`, `SQL`, `db_id`, `difficulty`). |
| `bird.smoke_subset(examples, n=50)` | → deterministic (seed=0) n-example subset. |
| `bird.schema_ddl(db_id)` | → `CREATE` statements for the DB (the schema handed to the agent). |
| `agent.generate_sql(question, evidence, schema_ddl, plan=None)` | → one SQLite string. Single Gemini call, no execution. Optional phase-3 `plan` (list of steps) is injected into the prompt. |
| `agent.repair_sql(question, evidence, schema_ddl, prev_sql, error)` | → corrected SQL, given the failed query and its DB error. |
| `executor.execute(sql, db_path)` | → `ExecResult(ok, rows, error)`. Never raises; failure → `ok=False` with the error string. |
| `explainer.explain(question, rows)` | → one plain-language sentence answering the question from the result rows. |
| `retriever.retrieve(question, evidence, db_id, top_k=5)` | → the `top_k` table names most relevant to the question, by embedding cosine similarity. Returns all tables when the schema has `<= top_k`. |
| `retriever.retrieve_scored(question, evidence, db_id, top_k=5)` | → `(name, cosine)` pairs for the kept tables (the gate's input). Pass-through schemas (`<= top_k`) score 1.0. |
| `retriever.retrieve_schema(question, evidence, db_id, top_k=5)` | → `(kept table names, pruned CREATE-statement DDL)` for those tables. |
| `planner.plan(question, evidence, schema_ddl)` | → 2–6 ordered plain-language steps for building the query. One Gemini call, no SQL. |
| `loop.solve(question, evidence, db_id, max_repairs=3, retrieve_top_k=None, use_planner=False, use_gate=False, abstain_threshold=None, trace=False)` | → `Solution` (final sql, exec result, attempts, per-attempt sqls/errors, explanation, retrieved `tables`, `plan`, `retrieval_scores`, and — when `use_gate` — `confidence`/`abstained`/`abstain_reason`/`gate_signals`; when `trace` — the phase-6 `trace`). Defaults = phase-2 loop; set `retrieve_top_k`/`use_planner`/`use_gate`/`trace` for phases 3/5/6. |
| `bird.gate_split(examples, n=100, seed=5)` | → deterministic n-example held-out split, disjoint from `smoke_subset` — the gate's eval set. |
| `score.ex_score(pred, gold, db_path)` | → 1 if result sets match, else 0. Mirrors official BIRD EX (error/timeout → 0). |
| `schema_link.tables_in_sql(sql)` | → lower-cased set of tables the query references (FROM + JOINs, comma joins, aliases, subqueries). |
| `schema_link.link_accuracy(pred_sql, gold_sql)` | → table-level precision/recall/f1 + missing/extra tables + `exact` flag. |
| `schema_link.retrieval_recall(retrieved, gold_sql)` | → retriever recall over gold tables + dropped tables; `None` when retrieval was off. |
| `taxonomy.tag_failure(row)` | → one cause tag for a run-file result row (`correct` when EX=1). Rule-based, offline. |
| `taxonomy.histogram(rows)` | → `{cause: count}` over rows, most common first. |
| `cost.cost_usd(model, prompt_tokens, output_tokens)` | → USD from the per-model price table (`NLSQL_PRICES` overrides). |
| `cost.run_cost(rows, model)` | → aggregate tokens/cost + latency mean/p50/p95 over run-file rows. |
| `judge.judge(question, pred_sql, gold_sql, explanation=None, ...)` | → `{correct, score, reason}`. One frontier-model call (offline-only). |
| `regression.compare_runs(old, new)` | → per-example EX diff: regressions (1→0), improvements (0→1), counts, delta. |
| `run_eval.evaluate(run, do_judge=False, ...)` | → full eval report (taxonomy + schema-linking + cost, +judge if set) for a loaded run. |
| `gate.confidence(row, max_repairs=3)` | → `{confidence, signals}` — the three schema-linking signals (retrieval/linking/execution) and their mean, from a run-file row. Rule-based, offline, no LLM. |
| `gate.decide(row, threshold=None, max_repairs=3)` | → `{confidence, abstain, reason, signals, threshold}`. Refuses below `config.ABSTAIN_THRESHOLD`; `reason` names the weakest signal. |
| `gate_eval.metrics(rows, threshold=None)` | → false-refusal / caught-error / abstention rate + served accuracy. `threshold=None` uses recorded decisions; a number recomputes from recorded `confidence`. |
| `gate_eval.sweep(rows, thresholds=None)` | → `metrics` recomputed across a threshold grid (default 0.0–1.0) from the recorded confidences — offline operating-point tuning. |
| `agent.reset_usage()` / `agent.get_usage()` | thread-local token/call counter so the runners attribute cost per example. |
| `trace.Trace.step(name, inputs)` | context manager that times one pipeline step and appends a `Step` (name, summarized inputs/output, latency, timestamp) to the trace. |
| `trace.Trace.link(answer, sql, tables, ddl)` | records the answer → final SQL → retrieved schema audit chain; `Trace.to_dict()` serializes steps + link to JSON. |
| `review.sort_rows(rows)` | run rows sorted ascending by gate `confidence` (lowest first, un-scored last). |
| `review.render(run)` | → a self-contained HTML review page (trace + SQL + retrieved schema + confidence + decision + answer per example), low-confidence-first. Offline. |

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # set GEMINI_API_KEY, or use the Vertex ADC vars

cd src
../.venv/bin/python -m nlsql.run_baseline            # phase 1: single-pass, 50-example subset
../.venv/bin/python -m nlsql.run_loop                # phase 2: executor loop + explainer
../.venv/bin/python -m nlsql.run_loop --limit 0      # full dev set (1534)
../.venv/bin/python -m nlsql.run_phase3              # phase 3: retriever + planner, full dev set
../.venv/bin/python -m nlsql.run_phase3 --limit 50   # phase 3 on the smoke subset
../.venv/bin/python -m nlsql.run_eval runs/phase3_<ts>.json   # phase 4: taxonomy + schema-links + cost (offline)
../.venv/bin/python -m nlsql.run_eval runs/phase3_<ts>.json --judge   # + frontier LLM judge (needs API)
../.venv/bin/python -m nlsql.regression runs/old.json runs/new.json   # phase 4: per-example regression diff
../.venv/bin/python -m nlsql.run_phase5              # phase 5: abstention gate on its held-out split
../.venv/bin/python -m nlsql.run_phase5 --threshold 0.6   # override the abstain threshold
../.venv/bin/python -m nlsql.gate_eval runs/phase5_<ts>.json   # phase 5: false-refusal / caught-error + threshold sweep (offline)
../.venv/bin/python -m nlsql.review runs/phase5_<ts>.json   # phase 6: static HTML review view, low-confidence-first (offline)
../.venv/bin/python -m nlsql.score                   # scorer self-check (no API/data needed)
../.venv/bin/python -m nlsql.executor                # executor self-check (no API/data needed)
../.venv/bin/python -m nlsql.retriever               # retriever self-check (no API/data needed)
../.venv/bin/python -m nlsql.planner                 # planner self-check (no API/data needed)
../.venv/bin/python -m nlsql.schema_link             # schema-linking self-check (no API/data needed)
../.venv/bin/python -m nlsql.taxonomy                # failure-taxonomy self-check (no API/data needed)
../.venv/bin/python -m nlsql.cost                    # cost/latency self-check (no API/data needed)
../.venv/bin/python -m nlsql.judge                   # LLM-judge parser self-check (no API/data needed)
../.venv/bin/python -m nlsql.regression _selfcheck   # regression self-check (no API/data needed)
../.venv/bin/python -m nlsql.run_eval _selfcheck     # eval-suite self-check (no API/data needed)
../.venv/bin/python -m nlsql.gate                    # abstention-gate self-check (no API/data needed)
../.venv/bin/python -m nlsql.gate_eval _selfcheck    # gate-eval self-check (no API/data needed)
../.venv/bin/python -m nlsql.run_phase5 _selfcheck   # gate-split self-check (no API/data needed)
../.venv/bin/python -m nlsql.trace                   # trace self-check (no API/data needed)
../.venv/bin/python -m nlsql.review _selfcheck       # review-view self-check (no API/data needed)
```

Outputs land in `runs/baseline_<ts>.json` / `runs/loop_<ts>.json` / `runs/phase3_<ts>.json` / `runs/phase5_<ts>.json` (per-example gold/pred/EX + latency/tokens + summary; phase 5 also records per-example gate `confidence` + `abstain`) and append to `runs/scores.log` (tagged `mode: baseline` / `mode: loop` / `mode: phase3` / `mode: phase5`). `run_eval` writes `runs/eval_<ts>.json`; `gate_eval` writes `runs/gate_eval_<ts>.json`. Phase-5 run rows also embed a per-example step `trace`; `review` renders any run file into `runs/review_<ts>.html`.

## Data setup (already done locally)

```bash
# from the parent dir:
git clone --depth 1 https://github.com/bird-bench/mini_dev.git bird-sql
curl -sL https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip -o bird-data/dev.zip
cd bird-data && unzip -q dev.zip && cd dev_20240627 && unzip -q dev_databases.zip
```

## Notes

- Credentials: AI Studio (`GEMINI_API_KEY`) or Vertex ADC (`GOOGLE_GENAI_USE_VERTEXAI=true` + project). google-genai auto-detects.
- The generator gets question + BIRD `evidence` (external knowledge) + schema DDL — evidence grounding is the point of BIRD.
- Embeddings and the LLM share one google-genai client; credentials are the same. Set `NLSQL_EMBED_MODEL` / `NLSQL_RETRIEVE_TOP_K` to override the retriever defaults.
- Phase-4 knobs: `NLSQL_JUDGE_MODEL` (default `gemini-2.5-pro`, frontier/offline) picks the judge; `NLSQL_PRICES` (JSON `{"model": {"input": usd, "output": usd}}` per 1M tokens) overrides the cost table.
- Phase-5 knob: `NLSQL_ABSTAIN_THRESHOLD` (default `0.55`) sets the confidence below which the gate refuses. The gate is our addition — BIRD does not reward abstention — so it is scored on its own held-out split (`bird.gate_split`) with false-refusal / caught-error, not against EX.
- ADK is still deferred. The phase-3 planner *decomposes* the question in a single up-front call — it does not choose tools or branch — so the whole pipeline (retrieve → plan → generate → execute → repair) stays a *fixed* plain-Python loop: cheaper and directly testable (each component ships a `_selfcheck`). ADK earns its place only once the agent actually selects steps at runtime.
