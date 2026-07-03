"""Phase-4 eval: analyze a run file — failure taxonomy, schema-linking accuracy,
cost/latency, and (opt-in) the LLM judge. Consumes a run JSON produced by
run_baseline / run_loop / run_phase3; writes runs/eval_<ts>.json next to it.

    python -m nlsql.run_eval runs/phase3_<ts>.json            # offline: taxonomy + links + cost
    python -m nlsql.run_eval runs/phase3_<ts>.json --judge     # also run the frontier LLM judge
    python -m nlsql.run_eval runs/phase3_<ts>.json --judge-limit 50

For regressions between two runs, see nlsql.regression.
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import config, cost, schema_link, taxonomy


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 4) if xs else 0.0


def _schema_linking(rows: list[dict]) -> dict:
    """Table-level schema-linking accuracy over rows that have both pred and gold SQL."""
    accs = [schema_link.link_accuracy(r.get("pred", ""), r["gold"]) for r in rows if r.get("gold")]
    rr = [schema_link.retrieval_recall(r.get("retrieved_tables"), r["gold"]) for r in rows if r.get("gold")]
    rr = [x for x in rr if x is not None]
    out = {
        "n": len(accs),
        "table_f1_mean": _mean([a["f1"] for a in accs]),
        "table_recall_mean": _mean([a["recall"] for a in accs]),
        "exact_table_match_rate": _mean([1.0 if a["exact"] else 0.0 for a in accs]),
    }
    if rr:  # only meaningful for phase-3 runs that recorded retrieved_tables
        out["retriever_recall_mean"] = _mean([x["recall"] for x in rr])
        out["retriever_covers_all_rate"] = _mean([1.0 if x["covers_all"] else 0.0 for x in rr])
    return out


def evaluate(run: dict, do_judge: bool = False, judge_limit: int = 0, model: str | None = None) -> dict:
    """Full eval report for a loaded run dict. Offline unless do_judge is set."""
    rows = run.get("results", [])
    run_model = run.get("summary", {}).get("model", config.MODEL)

    report = {
        "n": len(rows),
        "ex_overall": run.get("summary", {}).get("ex_overall"),
        "failure_taxonomy": taxonomy.histogram(rows),
        "schema_linking": _schema_linking(rows),
        "cost": cost.run_cost(rows, run_model),
    }
    if do_judge:
        report["judge"] = _run_judge(rows, judge_limit, model)
    return report


def _run_judge(rows: list[dict], limit: int, model: str | None) -> dict:
    """Judge the failing (EX=0) examples; flag ones the judge thinks were actually right."""
    from . import judge  # imported lazily — only this path needs the API

    failing = [r for r in rows if r.get("ex") == 0]
    if limit:
        failing = failing[:limit]

    def _j(r: dict) -> dict:
        v = judge.judge(r["question"], r.get("pred", ""), r["gold"], r.get("explanation"), model=model)
        return {"question_id": r["question_id"], **v}

    with ThreadPoolExecutor(max_workers=4) as pool:
        verdicts = list(pool.map(_j, failing))
    rescued = [v for v in verdicts if v["correct"]]  # EX=0 but judge says it answered the question
    return {
        "judged": len(verdicts),
        "judge_correct": len(rescued),  # candidate false-negatives of the EX metric
        "rescued_question_ids": [v["question_id"] for v in rescued],
        "verdicts": verdicts,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Phase-4 eval over a run file")
    p.add_argument("run", help="run JSON from run_baseline/run_loop/run_phase3")
    p.add_argument("--judge", action="store_true", help="also run the LLM judge (needs API)")
    p.add_argument("--judge-limit", type=int, default=0, help="cap judged failures; 0 = all")
    p.add_argument("--model", default=None, help="override judge model (default config.JUDGE_MODEL)")
    args = p.parse_args()

    run = json.loads(open(args.run).read())
    report = evaluate(run, args.judge, args.judge_limit, args.model)

    config.RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = config.RUNS_DIR / f"eval_{stamp}.json"
    out.write_text(json.dumps({"run_file": args.run, **report}, indent=2))
    print(json.dumps(report, indent=2))
    print(f"wrote {out}")


def _selfcheck():
    run = {
        "summary": {"ex_overall": 33.33, "model": "gemini-2.5-flash"},
        "results": [
            {"question_id": 1, "ex": 1, "pred": "SELECT x FROM a", "gold": "SELECT x FROM a",
             "prompt_tokens": 1_000_000, "output_tokens": 0, "llm_calls": 1, "latency_s": 2.0,
             "retrieved_tables": ["a"]},
            {"question_id": 2, "ex": 0, "pred": "SELECT x FROM b", "gold": "SELECT x FROM a",
             "exec_ok": True, "prompt_tokens": 0, "output_tokens": 0, "llm_calls": 1,
             "latency_s": 1.0, "retrieved_tables": ["b"]},
            {"question_id": 3, "ex": 0, "pred": "", "error": "boom", "gold": "SELECT x FROM a",
             "latency_s": 0.5, "retrieved_tables": ["a"]},
        ],
    }
    rep = evaluate(run)
    assert rep["failure_taxonomy"] == {"correct": 1, "execution_error": 1, "schema_linking": 1}, rep
    assert rep["schema_linking"]["n"] == 3, rep
    assert rep["schema_linking"]["exact_table_match_rate"] == round(1 / 3, 4), rep  # only row 1
    assert rep["schema_linking"]["retriever_recall_mean"] == round(2 / 3, 4), rep  # row 2 dropped 'a'
    assert abs(rep["cost"]["est_cost_usd"] - 0.30) < 1e-9, rep  # 1M input * 0.30/1M
    assert "judge" not in rep
    print("run_eval selfcheck ok")


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 2 and sys.argv[1] == "_selfcheck":
        _selfcheck()
    else:
        main()
