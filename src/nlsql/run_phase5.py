"""Phase-5 run: the abstention gate on its own held-out split. Runs the phase-3 pipeline
(retrieve -> plan -> generate -> execute -> repair) with the gate enabled, records the
gate's confidence + refusal per example, and scores the gate against EX — false-refusal
rate and caught-error rate. The gate is our addition; BIRD does not reward it.

    python -m nlsql.run_phase5                 # 100-example gate split (disjoint from smoke)
    python -m nlsql.run_phase5 --n 50          # smaller split
    python -m nlsql.run_phase5 --threshold 0.6 # override the abstain threshold
    python -m nlsql.run_phase5 --no-plan       # retriever only

For a threshold sweep / re-scoring an existing run offline, see nlsql.gate_eval.
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import agent, bird, config, cost, gate_eval, loop, score


def _one(ex: dict, model: str, max_repairs: int, top_k: int, use_planner: bool,
         threshold: float) -> dict:
    """Solve with the gate on + score a single example. Never raises — records the error."""
    row = {
        "question_id": ex["question_id"],
        "db_id": ex["db_id"],
        "difficulty": ex.get("difficulty", "unknown"),
        "question": ex["question"],
        "gold": ex["SQL"],
    }
    agent.reset_usage()
    t0 = time.time()
    try:
        sol = loop.solve(
            ex["question"], ex.get("evidence", ""), ex["db_id"], max_repairs, model,
            retrieve_top_k=top_k, use_planner=use_planner,
            use_gate=True, abstain_threshold=threshold, trace=True,
        )
        row["pred"] = sol.sql
        row["attempts"] = sol.attempts
        row["repaired"] = sol.attempts > 1
        row["exec_ok"] = sol.result.ok
        row["repair_errors"] = sol.errors
        row["retrieved_tables"] = sol.tables
        row["retrieval_scores"] = sol.retrieval_scores
        row["plan"] = sol.plan
        row["explanation"] = None if sol.abstained else sol.explanation  # refused -> no answer
        row["confidence"] = sol.confidence
        row["abstain"] = sol.abstained
        row["gate_reason"] = sol.abstain_reason
        row["gate_signals"] = sol.gate_signals
        row["trace"] = sol.trace  # phase-6 step trace (answer -> sql -> retrieved schema)
        row["ex"] = score.ex_score(sol.sql, ex["SQL"], str(config.db_path(ex["db_id"])))
    except Exception as e:
        row["pred"] = ""
        row["ex"] = 0
        row["error"] = str(e)
    row["latency_s"] = round(time.time() - t0, 3)
    row.update(agent.get_usage())
    return row


def _log_abstentions(rows: list[dict]) -> None:
    """Log every abstention (plan.md: "log every abstention")."""
    for r in rows:
        if r.get("abstain"):
            print(f"  ABSTAIN  qid={r['question_id']}  db={r['db_id']}  "
                  f"conf={r.get('confidence')}  {r.get('gate_reason')}")


def _summary(rows: list[dict], threshold: float) -> dict:
    overall = sum(r["ex"] for r in rows) / len(rows) * 100
    return {
        "n": len(rows),
        "ex_overall": round(overall, 2),
        "abstain_threshold": threshold,
        "n_abstained": sum(r.get("abstain", False) for r in rows),
        "gate": gate_eval.metrics(rows),
        "gate_sweep": gate_eval.sweep(rows),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="BIRD phase-5 abstention gate on its held-out split")
    p.add_argument("--n", type=int, default=100, help="gate-split size (disjoint from smoke subset)")
    p.add_argument("--workers", type=int, default=4, help="concurrent solve calls")
    p.add_argument("--max-repairs", type=int, default=3, help="repair retries per example")
    p.add_argument("--top-k", type=int, default=config.RETRIEVE_TOP_K, help="tables kept by the retriever")
    p.add_argument("--threshold", type=float, default=config.ABSTAIN_THRESHOLD, help="abstain below this confidence")
    p.add_argument("--no-plan", dest="plan", action="store_false", help="skip the planner")
    p.add_argument("--model", default=config.MODEL)
    args = p.parse_args()

    examples = bird.load_examples()
    subset = bird.gate_split(examples, args.n)
    print(
        f"model={args.model}  gate_split={len(subset)}  workers={args.workers}  "
        f"top_k={args.top_k}  planner={args.plan}  abstain_threshold={args.threshold}"
    )

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(
            lambda ex: _one(ex, args.model, args.max_repairs, args.top_k, args.plan, args.threshold),
            subset,
        ))
    elapsed = round(time.time() - t0, 1)

    summary = _summary(rows, args.threshold) | {
        "model": args.model, "top_k": args.top_k, "planner": args.plan, "elapsed_s": elapsed,
        "cost": cost.run_cost(rows, args.model),
    }
    errors = sum("error" in r for r in rows)

    config.RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = config.RUNS_DIR / f"phase5_{stamp}.json"
    out.write_text(json.dumps({"summary": summary, "results": rows}, indent=2))

    _log_abstentions(rows)
    print(json.dumps(summary, indent=2))
    print(f"generation errors: {errors}")
    print(f"wrote {out}")
    with open(config.RUNS_DIR / "scores.log", "a") as f:
        f.write(json.dumps({"time": stamp, "mode": "phase5", **summary, "errors": errors}) + "\n")


def _selfcheck():
    # gate_split is deterministic and disjoint from the smoke subset.
    examples = [{"question_id": i} for i in range(400)]
    g1 = bird.gate_split(examples, 100)
    g2 = bird.gate_split(examples, 100)
    assert [e["question_id"] for e in g1] == [e["question_id"] for e in g2], "not deterministic"
    assert len(g1) == 100, len(g1)
    smoke = {e["question_id"] for e in bird.smoke_subset(examples, 50)}
    assert not (smoke & {e["question_id"] for e in g1}), "gate split overlaps smoke subset"
    print("run_phase5 selfcheck ok")


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 2 and sys.argv[1] == "_selfcheck":
        _selfcheck()
    else:
        main()
