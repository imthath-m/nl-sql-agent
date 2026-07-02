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
| `agent.generate_sql(question, evidence, schema_ddl)` | → one SQLite string. Single Gemini call, no execution. |
| `agent.repair_sql(question, evidence, schema_ddl, prev_sql, error)` | → corrected SQL, given the failed query and its DB error. |
| `executor.execute(sql, db_path)` | → `ExecResult(ok, rows, error)`. Never raises; failure → `ok=False` with the error string. |
| `explainer.explain(question, rows)` | → one plain-language sentence answering the question from the result rows. |
| `loop.solve(question, evidence, db_id, max_repairs=3)` | → `Solution` (final sql, exec result, attempts, per-attempt sqls/errors, explanation). The phase-2 loop. |
| `score.ex_score(pred, gold, db_path)` | → 1 if result sets match, else 0. Mirrors official BIRD EX (error/timeout → 0). |

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # set GEMINI_API_KEY, or use the Vertex ADC vars

cd src
../.venv/bin/python -m nlsql.run_baseline            # phase 1: single-pass, 50-example subset
../.venv/bin/python -m nlsql.run_loop                # phase 2: executor loop + explainer
../.venv/bin/python -m nlsql.run_loop --limit 0      # full dev set (1534)
../.venv/bin/python -m nlsql.score                   # scorer self-check (no API/data needed)
../.venv/bin/python -m nlsql.executor                # executor self-check (no API/data needed)
```

Outputs land in `runs/baseline_<ts>.json` / `runs/loop_<ts>.json` (per-example gold/pred/EX + summary) and append to `runs/scores.log` (tagged `mode: baseline` / `mode: loop`).

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
- ADK is still deferred. Phase 2's control flow (generate → execute → feed the error back → retry to a cap) is a *fixed* loop, not autonomous tool-selection, so it's plain Python — cheaper and directly testable (`executor._selfcheck`). ADK earns its place in phase 3, where the planner actually chooses steps.
