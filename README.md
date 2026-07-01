# nl-sql-agent

NL→SQL analytics agent. Phase 1: BIRD-SQL baseline. See [plan.md](plan.md) for the roadmap.

## Phase 1 — status: done

Single-pass Gemini Flash generator scored with the official BIRD execution-accuracy (EX) metric.

**Baseline: 66% EX** on the 50-example smoke subset (`gemini-2.5-flash`, 0 errors). Latest run in `runs/`.

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
| `score.ex_score(pred, gold, db_path)` | → 1 if result sets match, else 0. Mirrors official BIRD EX (error/timeout → 0). |

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # set GEMINI_API_KEY, or use the Vertex ADC vars

cd src
../.venv/bin/python -m nlsql.run_baseline            # 50-example smoke subset
../.venv/bin/python -m nlsql.run_baseline --limit 0  # full dev set (1534)
../.venv/bin/python -m nlsql.score                   # scorer self-check
```

Outputs land in `runs/baseline_<ts>.json` (per-example gold/pred/EX + summary) and append to `runs/scores.log`.

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
- ADK is deferred to phase 2, where the executor loop (tools, retries) actually needs an agent framework. Phase 1 is a single generate call; wrapping it in ADK now would be scaffolding for later.
