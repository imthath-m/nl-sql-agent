"""Phase-3 run: schema retriever + planner in front of the executor loop, on the full BIRD
dev set. Pipeline per example: retrieve relevant tables -> plan steps -> generate -> execute
-> repair -> explain. Scores EX, logs the delta vs the phase-1 baseline.

    python -m nlsql.run_phase3               # full dev set (1534)
    python -m nlsql.run_phase3 --limit 50    # smoke subset
    python -m nlsql.run_phase3 --no-plan     # retriever only
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import bird, config, loop, score


def _one(ex: dict, model: str, max_repairs: int, top_k: int, use_planner: bool) -> dict:
    """Solve + score a single example. Never raises — records the error instead."""
    row = {
        "question_id": ex["question_id"],
        "db_id": ex["db_id"],
        "difficulty": ex.get("difficulty", "unknown"),
        "question": ex["question"],
        "gold": ex["SQL"],
    }
    try:
        sol = loop.solve(
            ex["question"], ex.get("evidence", ""), ex["db_id"], max_repairs, model,
            retrieve_top_k=top_k, use_planner=use_planner,
        )
        row["pred"] = sol.sql
        row["attempts"] = sol.attempts
        row["repaired"] = sol.attempts > 1
        row["exec_ok"] = sol.result.ok
        row["repair_errors"] = sol.errors
        row["retrieved_tables"] = sol.tables
        row["plan"] = sol.plan
        row["explanation"] = sol.explanation
        row["ex"] = score.ex_score(sol.sql, ex["SQL"], str(config.db_path(ex["db_id"])))
    except Exception as e:
        row["pred"] = ""
        row["ex"] = 0
        row["error"] = str(e)
    return row


def _summary(rows: list[dict]) -> dict:
    by_diff: dict[str, list[int]] = {}
    for r in rows:
        by_diff.setdefault(r["difficulty"], []).append(r["ex"])
    overall = sum(r["ex"] for r in rows) / len(rows) * 100
    return {
        "n": len(rows),
        "ex_overall": round(overall, 2),
        "ex_by_difficulty": {d: round(sum(v) / len(v) * 100, 2) for d, v in sorted(by_diff.items())},
        "counts_by_difficulty": {d: len(v) for d, v in sorted(by_diff.items())},
        "repaired": sum(r.get("repaired", False) for r in rows),
        "exec_fail": sum(r.get("exec_ok") is False for r in rows),
    }


def _last_ex(mode: str) -> float | None:
    """ex_overall of the most recent run tagged `mode` in scores.log, for the delta."""
    log = config.RUNS_DIR / "scores.log"
    if not log.exists():
        return None
    best = None
    for line in log.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("mode") == mode:
            best = rec.get("ex_overall")
    return best


def main() -> None:
    p = argparse.ArgumentParser(description="BIRD phase-3 retriever + planner loop")
    p.add_argument("--limit", type=int, default=0, help="subset size; 0 = full dev set (1534)")
    p.add_argument("--workers", type=int, default=4, help="concurrent solve calls")
    p.add_argument("--max-repairs", type=int, default=3, help="repair retries per example")
    p.add_argument("--top-k", type=int, default=config.RETRIEVE_TOP_K, help="tables kept by the retriever")
    p.add_argument("--no-plan", dest="plan", action="store_false", help="skip the planner")
    p.add_argument("--model", default=config.MODEL)
    args = p.parse_args()

    examples = bird.load_examples()
    subset = examples if args.limit == 0 else bird.smoke_subset(examples, args.limit)
    print(
        f"model={args.model}  examples={len(subset)}  workers={args.workers}  "
        f"max_repairs={args.max_repairs}  top_k={args.top_k}  planner={args.plan}"
    )

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(
            lambda ex: _one(ex, args.model, args.max_repairs, args.top_k, args.plan), subset
        ))
    elapsed = round(time.time() - t0, 1)

    summary = _summary(rows) | {
        "model": args.model, "top_k": args.top_k, "planner": args.plan, "elapsed_s": elapsed,
    }
    errors = sum("error" in r for r in rows)

    for mode in ("baseline", "loop"):
        prev = _last_ex(mode)
        if prev is not None:
            summary[f"ex_delta_vs_{mode}"] = round(summary["ex_overall"] - prev, 2)

    config.RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = config.RUNS_DIR / f"phase3_{stamp}.json"
    out.write_text(json.dumps({"summary": summary, "results": rows}, indent=2))

    print(json.dumps(summary, indent=2))
    print(f"generation errors: {errors}")
    print(f"wrote {out}")
    with open(config.RUNS_DIR / "scores.log", "a") as f:
        f.write(json.dumps({"time": stamp, "mode": "phase3", **summary, "errors": errors}) + "\n")


if __name__ == "__main__":
    main()
