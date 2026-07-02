"""Phase-2 run: solve the smoke subset with the executor loop (generate -> execute ->
repair -> explain), score EX, and report the delta vs the phase-1 baseline.

    python -m nlsql.run_loop            # 50-example smoke subset
    python -m nlsql.run_loop --limit 0  # full dev set (1534)
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import bird, config, loop, score


def _one(ex: dict, model: str, max_repairs: int) -> dict:
    """Solve + score a single example. Never raises — records the error instead."""
    row = {
        "question_id": ex["question_id"],
        "db_id": ex["db_id"],
        "difficulty": ex.get("difficulty", "unknown"),
        "question": ex["question"],
        "gold": ex["SQL"],
    }
    try:
        sol = loop.solve(ex["question"], ex.get("evidence", ""), ex["db_id"], max_repairs, model)
        row["pred"] = sol.sql
        row["attempts"] = sol.attempts
        row["repaired"] = sol.attempts > 1
        row["exec_ok"] = sol.result.ok
        row["repair_errors"] = sol.errors
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


def _last_baseline_ex() -> float | None:
    """ex_overall of the most recent baseline run in scores.log, for the delta. None if absent."""
    log = config.RUNS_DIR / "scores.log"
    if not log.exists():
        return None
    best = None
    for line in log.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("mode") == "baseline":
            best = rec.get("ex_overall")
    return best


def main() -> None:
    p = argparse.ArgumentParser(description="BIRD phase-2 executor loop")
    p.add_argument("--limit", type=int, default=50, help="subset size; 0 = full dev set")
    p.add_argument("--workers", type=int, default=4, help="concurrent solve calls")
    p.add_argument("--max-repairs", type=int, default=3, help="repair retries per example")
    p.add_argument("--model", default=config.MODEL)
    args = p.parse_args()

    examples = bird.load_examples()
    subset = examples if args.limit == 0 else bird.smoke_subset(examples, args.limit)
    print(f"model={args.model}  examples={len(subset)}  workers={args.workers}  max_repairs={args.max_repairs}")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(lambda ex: _one(ex, args.model, args.max_repairs), subset))
    elapsed = round(time.time() - t0, 1)

    summary = _summary(rows) | {"model": args.model, "elapsed_s": elapsed}
    errors = sum("error" in r for r in rows)

    baseline_ex = _last_baseline_ex()
    if baseline_ex is not None:
        summary["ex_delta_vs_baseline"] = round(summary["ex_overall"] - baseline_ex, 2)

    config.RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = config.RUNS_DIR / f"loop_{stamp}.json"
    out.write_text(json.dumps({"summary": summary, "results": rows}, indent=2))

    print(json.dumps(summary, indent=2))
    print(f"generation errors: {errors}")
    if baseline_ex is None:
        print("no baseline in scores.log yet — run nlsql.run_baseline for a delta")
    print(f"wrote {out}")
    with open(config.RUNS_DIR / "scores.log", "a") as f:
        f.write(json.dumps({"time": stamp, "mode": "loop", **summary, "errors": errors}) + "\n")


if __name__ == "__main__":
    main()
