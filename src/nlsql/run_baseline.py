"""Phase-1 baseline: generate SQL for the smoke subset, score EX, log the result.

    python -m nlsql.run_baseline            # 50-example smoke subset
    python -m nlsql.run_baseline --limit 0  # full dev set (1534)
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import agent, bird, config, score


def _one(ex: dict, model: str) -> dict:
    """Generate + score a single example. Never raises — records the error instead."""
    row = {
        "question_id": ex["question_id"],
        "db_id": ex["db_id"],
        "difficulty": ex.get("difficulty", "unknown"),
        "question": ex["question"],
        "gold": ex["SQL"],
    }
    try:
        pred = agent.generate_sql(ex["question"], ex.get("evidence", ""), bird.schema_ddl(ex["db_id"]), model)
        row["pred"] = pred
        row["ex"] = score.ex_score(pred, ex["SQL"], str(config.db_path(ex["db_id"])))
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
    }


def main() -> None:
    p = argparse.ArgumentParser(description="BIRD phase-1 baseline")
    p.add_argument("--limit", type=int, default=50, help="subset size; 0 = full dev set")
    p.add_argument("--workers", type=int, default=4, help="concurrent generation calls")
    p.add_argument("--model", default=config.MODEL)
    args = p.parse_args()

    examples = bird.load_examples()
    subset = examples if args.limit == 0 else bird.smoke_subset(examples, args.limit)
    print(f"model={args.model}  examples={len(subset)}  workers={args.workers}")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(lambda ex: _one(ex, args.model), subset))
    elapsed = round(time.time() - t0, 1)

    summary = _summary(rows) | {"model": args.model, "elapsed_s": elapsed}
    errors = sum("error" in r for r in rows)

    config.RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = config.RUNS_DIR / f"baseline_{stamp}.json"
    out.write_text(json.dumps({"summary": summary, "results": rows}, indent=2))

    print(json.dumps(summary, indent=2))
    print(f"generation errors: {errors}")
    print(f"wrote {out}")
    with open(config.RUNS_DIR / "scores.log", "a") as f:
        f.write(json.dumps({"time": stamp, "mode": "baseline", **summary, "errors": errors}) + "\n")


if __name__ == "__main__":
    main()
