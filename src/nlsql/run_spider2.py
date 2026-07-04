"""Phase-7 run: the NL->SQL pipeline ported to Spider 2.0-Lite, on the held-in split.

Per example: build the dialect schema DDL (wide tables column-pruned) -> plan -> generate
(BigQuery/Snowflake/SQLite dialect) -> execute (locally for sqlite, via the warehouse adapter
otherwise) -> repair -> explain -> gate. EX is scored by comparing the result table against
the released gold-result CSV (spider2_score), not by re-running gold SQL. Writes a run file
whose rows carry the fields the phase-4 eval suite consumes, and logs `mode: spider2`.

    python -m nlsql.run_spider2                  # 50-example held-in split
    python -m nlsql.run_spider2 --n 20 --no-plan
    python -m nlsql.run_spider2 --platform sqlite  # only locally-executable examples

Live BigQuery/Snowflake execution needs the optional clients + credentials (see warehouse.py);
without them those rows record a clear "credentials required" error and score 0 — the port,
dialect prompts, and wide-schema pruning still run and are captured in the run file.
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import (agent, config, cost, explainer, gate, planner, spider2,
               spider2_score, warehouse)
from . import trace as _trace


def _pipeline(ex: dict, model: str, col_budget: int, max_repairs: int, use_planner: bool,
              use_gate: bool, threshold: float, do_trace: bool) -> dict:
    """Solve + score one Spider2-Lite example. Never raises — records the error instead."""
    iid, db, platform = ex["instance_id"], ex["db"], ex["platform"]
    dialect = platform  # dialect key == platform key (sqlite | bigquery | snowflake)
    row = {
        "question_id": iid,
        "instance_id": iid,
        "db_id": db,
        "platform": platform,
        "difficulty": "unknown",
        "question": ex["question"],
    }
    agent.reset_usage()
    tr = _trace.Trace()
    t0 = time.time()
    try:
        evidence = spider2.external_knowledge(ex)
        row["gold"] = spider2.gold_sql(iid)

        with tr.step("retrieve", {"db": db, "platform": platform, "col_budget": col_budget}) as s:
            tables = spider2.table_names(db, platform)
            schema = spider2.schema_ddl(db, platform, ex["question"], evidence, col_budget)
            s["output"] = {"tables": tables, "schema_chars": len(schema)}

        steps = None
        if use_planner:
            with tr.step("plan", {"question": ex["question"]}) as s:
                steps = planner.plan(ex["question"], evidence, schema, model)
                s["output"] = {"steps": steps}

        with tr.step("generate", {"dialect": dialect, "plan": steps}) as s:
            sql = agent.generate_sql(ex["question"], evidence, schema, model, plan=steps, dialect=dialect)
            s["output"] = {"sql": sql}
        sqls, errors = [sql], []
        with tr.step("execute", {"sql": sql}) as s:
            result = warehouse.execute(sql, ex)
            s["output"] = {"ok": result.ok, "rows": len(result.rows or []), "error": result.error}

        for _ in range(max_repairs):
            if result.ok:
                break
            errors.append(result.error)
            with tr.step("repair", {"error": result.error}) as s:
                sql = agent.repair_sql(ex["question"], evidence, schema, sql, result.error,
                                       model, dialect=dialect)
                s["output"] = {"sql": sql}
            sqls.append(sql)
            with tr.step("execute", {"sql": sql}) as s:
                result = warehouse.execute(sql, ex)
                s["output"] = {"ok": result.ok, "rows": len(result.rows or []), "error": result.error}

        row["pred"] = sql
        row["attempts"] = len(sqls)
        row["repaired"] = len(sqls) > 1
        row["exec_ok"] = result.ok
        row["repair_errors"] = errors
        row["retrieved_tables"] = tables
        row["plan"] = steps

        # EX: compare the predicted result table against the gold-result CSV.
        gold_csv = spider2.gold_result_path(iid)
        if result.ok and gold_csv.exists():
            row["ex"] = spider2_score.score_csv(result.rows, str(gold_csv))
        else:
            row["ex"] = 0
            if not gold_csv.exists():
                row["error"] = f"no gold result CSV: {gold_csv.name}"

        explanation = None
        if result.ok:
            with tr.step("explain", {"rows": len(result.rows or [])}) as s:
                explanation = explainer.explain(ex["question"], result.rows, model)
                s["output"] = {"explanation": explanation}

        if use_gate:
            d = gate.decide(
                {"retrieval_scores": None, "retrieved_tables": tables, "pred": sql,
                 "attempts": len(sqls), "exec_ok": result.ok},
                threshold, max_repairs,
            )
            row["confidence"] = d["confidence"]
            row["abstain"] = d["abstain"]
            row["gate_reason"] = d["reason"] or None
            row["gate_signals"] = d["signals"]
            explanation = None if d["abstain"] else explanation
        row["explanation"] = explanation
    except Exception as e:
        row.setdefault("pred", "")
        row["ex"] = 0
        row["error"] = f"{type(e).__name__}: {e}"
    tr.link(answer=row.get("explanation"), sql=row.get("pred", ""),
            tables=row.get("retrieved_tables"), ddl=locals().get("schema", ""))
    if do_trace:
        row["trace"] = tr.to_dict()
    row["latency_s"] = round(time.time() - t0, 3)
    row.update(agent.get_usage())
    return row


def _summary(rows: list[dict]) -> dict:
    by_plat: dict[str, list[int]] = {}
    for r in rows:
        by_plat.setdefault(r["platform"], []).append(r["ex"])
    overall = sum(r["ex"] for r in rows) / len(rows) * 100 if rows else 0.0
    return {
        "n": len(rows),
        "ex_overall": round(overall, 2),
        "ex_by_platform": {p: round(sum(v) / len(v) * 100, 2) for p, v in sorted(by_plat.items())},
        "counts_by_platform": {p: len(v) for p, v in sorted(by_plat.items())},
        "exec_fail": sum(r.get("exec_ok") is False for r in rows),
        "n_abstained": sum(r.get("abstain", False) for r in rows),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Spider 2.0-Lite phase-7 run on the held-in split")
    p.add_argument("--n", type=int, default=50, help="held-in split size (0 = full set)")
    p.add_argument("--workers", type=int, default=4, help="concurrent solve calls")
    p.add_argument("--max-repairs", type=int, default=3, help="repair retries per example")
    p.add_argument("--col-budget", type=int, default=config.SPIDER2_COL_BUDGET,
                   help="max columns kept per table (wide-schema pruner)")
    p.add_argument("--platform", choices=["sqlite", "bigquery", "snowflake"],
                   help="restrict to one backend (e.g. sqlite to run without warehouse creds)")
    p.add_argument("--no-plan", dest="plan", action="store_false", help="skip the planner")
    p.add_argument("--no-gate", dest="gate", action="store_false", help="skip the abstention gate")
    p.add_argument("--threshold", type=float, default=config.ABSTAIN_THRESHOLD)
    p.add_argument("--model", default=config.MODEL)
    args = p.parse_args()

    examples = spider2.load_examples()
    if args.platform:
        examples = [e for e in examples if e["platform"] == args.platform]
    subset = examples if args.n == 0 else spider2.held_in_split(examples, args.n)
    print(
        f"model={args.model}  examples={len(subset)}  workers={args.workers}  "
        f"col_budget={args.col_budget}  planner={args.plan}  gate={args.gate}"
    )

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(
            lambda ex: _pipeline(ex, args.model, args.col_budget, args.max_repairs,
                                 args.plan, args.gate, args.threshold, True),
            subset,
        ))
    elapsed = round(time.time() - t0, 1)

    summary = _summary(rows) | {
        "model": args.model, "col_budget": args.col_budget, "planner": args.plan,
        "elapsed_s": elapsed, "cost": cost.run_cost(rows, args.model),
    }
    errors = sum("error" in r for r in rows)

    config.RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = config.RUNS_DIR / f"spider2_{stamp}.json"
    out.write_text(json.dumps({"summary": summary, "results": rows}, indent=2))

    print(json.dumps(summary, indent=2))
    print(f"errors: {errors}")
    print(f"wrote {out}")
    with open(config.RUNS_DIR / "scores.log", "a") as f:
        f.write(json.dumps({"time": stamp, "mode": "spider2", **summary, "errors": errors}) + "\n")


def _selfcheck():
    # Offline end-to-end on synthetic fixtures: build a tiny Spider2 SQLite example, stub the
    # LLM calls (agent/planner/explainer), and run the pipeline through to a scored row.
    import csv
    import sqlite3
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "spider2-lite"
        (root / "evaluation_suite" / "gold" / "sql").mkdir(parents=True)
        (root / "evaluation_suite" / "gold" / "exec_result").mkdir(parents=True)
        sq = root / "resource" / "databases" / "sqlite"
        sq.mkdir(parents=True)
        con = sqlite3.connect(str(sq / "shop.sqlite"))
        con.execute("CREATE TABLE orders(id INTEGER, total REAL)")
        con.executemany("INSERT INTO orders VALUES (?, ?)", [(1, 10.0), (2, 20.0)])
        con.commit()
        con.close()
        ex = {"instance_id": "local001", "db": "shop", "question": "count orders",
              "external_knowledge": None, "platform": "sqlite"}
        (root / "spider2-lite.jsonl").write_text(json.dumps(ex))
        (root / "evaluation_suite" / "gold" / "sql" / "local001.sql").write_text(
            "SELECT COUNT(*) FROM orders")
        with open(root / "evaluation_suite" / "gold" / "exec_result" / "local001.csv", "w",
                  newline="") as f:
            csv.writer(f).writerows([["cnt"], ["2"]])

        # stub the three LLM entry points so no API is needed
        orig = (agent.generate_sql, agent.repair_sql, planner.plan, explainer.explain)
        agent.generate_sql = lambda *a, **k: "SELECT COUNT(*) FROM orders"
        agent.repair_sql = lambda *a, **k: "SELECT COUNT(*) FROM orders"
        planner.plan = lambda *a, **k: ["count the orders"]
        explainer.explain = lambda *a, **k: "There are 2 orders."
        old = config.SPIDER2_DIR
        config.SPIDER2_DIR = root
        spider2.load_tables.cache_clear()
        try:
            row = _pipeline(ex, "stub", 40, 3, True, True, 0.55, True)
            assert row["exec_ok"] and row["ex"] == 1, row  # scored against the gold CSV
            assert row["pred"].startswith("SELECT COUNT") and row["plan"] == ["count the orders"]
            assert row["retrieved_tables"] == ["orders"], row
            assert "confidence" in row and row["abstain"] is False, row
            assert row["explanation"] == "There are 2 orders."
            assert row["trace"]["steps"][0]["name"] == "retrieve", row["trace"]
            s = _summary([row])
            assert s["ex_overall"] == 100.0 and s["ex_by_platform"] == {"sqlite": 100.0}, s
        finally:
            (agent.generate_sql, agent.repair_sql, planner.plan, explainer.explain) = orig
            config.SPIDER2_DIR = old
            spider2.load_tables.cache_clear()
    print("run_spider2 selfcheck ok")


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 2 and sys.argv[1] == "_selfcheck":
        _selfcheck()
    else:
        main()
