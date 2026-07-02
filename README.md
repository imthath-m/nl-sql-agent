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
../.venv/bin/python -m nlsql.score                   # scorer self-check (no API/data needed)
../.venv/bin/python -m nlsql.executor                # executor self-check (no API/data needed)
../.venv/bin/python -m nlsql.retriever               # retriever self-check (no API/data needed)
../.venv/bin/python -m nlsql.planner                 # planner self-check (no API/data needed)
```

Outputs land in `runs/baseline_<ts>.json` / `runs/loop_<ts>.json` / `runs/phase3_<ts>.json` (per-example gold/pred/EX + summary) and append to `runs/scores.log` (tagged `mode: baseline` / `mode: loop` / `mode: phase3`).

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
- ADK is still deferred. The phase-3 planner *decomposes* the question in a single up-front call — it does not choose tools or branch — so the whole pipeline (retrieve → plan → generate → execute → repair) stays a *fixed* plain-Python loop: cheaper and directly testable (each component ships a `_selfcheck`). ADK earns its place only once the agent actually selects steps at runtime.
