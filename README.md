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
| `retriever.retrieve_schema(question, evidence, db_id, top_k=5)` | → `(kept table names, pruned CREATE-statement DDL)` for those tables. |
| `planner.plan(question, evidence, schema_ddl)` | → 2–6 ordered plain-language steps for building the query. One Gemini call, no SQL. |
| `loop.solve(question, evidence, db_id, max_repairs=3, retrieve_top_k=None, use_planner=False)` | → `Solution` (final sql, exec result, attempts, per-attempt sqls/errors, explanation, retrieved `tables`, `plan`). Defaults = phase-2 loop; set `retrieve_top_k`/`use_planner` for phase 3. |
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
| `agent.reset_usage()` / `agent.get_usage()` | thread-local token/call counter so the runners attribute cost per example. |

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
```

Outputs land in `runs/baseline_<ts>.json` / `runs/loop_<ts>.json` / `runs/phase3_<ts>.json` (per-example gold/pred/EX + latency/tokens + summary) and append to `runs/scores.log` (tagged `mode: baseline` / `mode: loop` / `mode: phase3`). `run_eval` writes `runs/eval_<ts>.json`.

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
- ADK is still deferred. The phase-3 planner *decomposes* the question in a single up-front call — it does not choose tools or branch — so the whole pipeline (retrieve → plan → generate → execute → repair) stays a *fixed* plain-Python loop: cheaper and directly testable (each component ships a `_selfcheck`). ADK earns its place only once the agent actually selects steps at runtime.
