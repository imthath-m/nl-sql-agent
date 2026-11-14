"""Judge-vs-EX agreement check: run the LLM judge on every row of one run file.

    python -m nlsql.judge_check                    # held-out run of the frozen tune winner
    python -m nlsql.judge_check --run results/run_all/phase3.json --limit 50
    python -m nlsql.judge_check _selfcheck         # offline, stubbed judge

Writes <out>/verdicts.json and <out>/report.md: confusion matrix (judge vs EX), agreement,
Cohen's kappa, both disagreement types, judge token cost and 10 disagreement examples.
Verdicts checkpoint to <out>/verdicts.rows.jsonl; rows with `error` are retried on resume.
"""
import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import agent, config, cost, stats

DEFAULT_OUT = config.REPO_ROOT / "results" / "judge"
TUNE_DIR = config.REPO_ROOT / "results" / "tune"


def default_run() -> Path:
    """Held-out run file of the frozen tune winner (results/tune/winner.json)."""
    w = json.loads((TUNE_DIR / "winner.json").read_text())
    return TUNE_DIR / f"heldout_{w['winner']}.json"


def load_rows(path: Path) -> list[dict]:
    """Rows from a run JSON ({"results": [...]}) or a .rows.jsonl checkpoint (last row per qid)."""
    text = path.read_text()
    if path.suffix == ".jsonl":
        by_q: dict = {}
        for line in text.splitlines():
            if line.strip():
                r = json.loads(line)
                by_q[r["question_id"]] = r
        return list(by_q.values())
    return json.loads(text)["results"]


def _pred(r: dict) -> str:
    return r.get("pred") or r.get("pred_sql") or ""


def _one(r: dict, evidence: dict, model: str, judge_fn: Callable) -> dict:
    """Judge one row. Never raises: failures are recorded as `error`."""
    v = {"question_id": r["question_id"], "question": r.get("question", ""), "gold": r.get("gold", ""),
         "pred": _pred(r), "ex": int(r.get("ex", 0))}
    agent.reset_usage()
    try:
        out = judge_fn(r.get("question", ""), _pred(r), r.get("gold", ""),
                       explanation=r.get("explanation"),
                       evidence=evidence.get(r["question_id"], r.get("evidence", "")), model=model)
        v.update(judge_correct=bool(out["correct"]), judge_score=out.get("score"),
                 judge_reason=out.get("reason", ""))
    except Exception as e:
        v["error"] = str(e)
    v.update(agent.get_usage())
    return v


def run_judge(rows: list[dict], evidence: dict, out: Path, model: str, workers: int,
              judge_fn: Callable) -> list[dict]:
    ckpt = out / "verdicts.rows.jsonl"
    done: dict = {}
    if ckpt.exists():
        for line in ckpt.read_text().splitlines():
            if line.strip():
                v = json.loads(line)
                if "error" not in v and v.get("model") == model:
                    done[v["question_id"]] = v
    todo = [r for r in rows if r["question_id"] not in done]
    print(f"judge: {len(done)} checkpointed, {len(todo)} to run", flush=True)
    lock = threading.Lock()

    def work(r):
        v = {**_one(r, evidence, model, judge_fn), "model": model}
        with lock, open(ckpt, "a") as f:
            f.write(json.dumps(v) + "\n")
        return v

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for v in pool.map(work, todo):
            done[v["question_id"]] = v
    return [done[r["question_id"]] for r in rows]


def analyze(verdicts: list[dict], model: str, n_examples: int = 10) -> dict:
    ok = [v for v in verdicts if "error" not in v]
    tp = sum(v["ex"] == 1 and v["judge_correct"] for v in ok)
    fn = sum(v["ex"] == 1 and not v["judge_correct"] for v in ok)
    fp = sum(v["ex"] == 0 and v["judge_correct"] for v in ok)
    tn = sum(v["ex"] == 0 and not v["judge_correct"] for v in ok)
    n = len(ok)
    a = sorted((v for v in ok if v["ex"] == 0 and v["judge_correct"]), key=lambda v: v["question_id"])
    b = sorted((v for v in ok if v["ex"] == 1 and not v["judge_correct"]), key=lambda v: v["question_id"])
    # balanced, deterministic: alternate types, then fill from whichever has more
    picked, i = [], 0
    while len(picked) < n_examples and (i < len(a) or i < len(b)):
        for lst in (a, b):
            if i < len(lst) and len(picked) < n_examples:
                picked.append(lst[i])
        i += 1
    pt = sum(v.get("prompt_tokens", 0) for v in verdicts)
    ot = sum(v.get("output_tokens", 0) for v in verdicts)
    return {
        "n_rows": len(verdicts), "n_judged": n, "n_errors": len(verdicts) - n,
        "confusion": {"ex1_judge_correct": tp, "ex1_judge_wrong": fn,
                      "ex0_judge_correct": fp, "ex0_judge_wrong": tn},
        "agreement": round((tp + tn) / n, 4) if n else 0.0,
        "cohen_kappa": round(stats.cohen_kappa(tp, fn, fp, tn), 4),
        "disagree_ex0_judge_correct": fp, "disagree_ex1_judge_wrong": fn,
        "judge_cost": {"model": model, "prompt_tokens": pt, "output_tokens": ot,
                       "llm_calls": sum(v.get("llm_calls", 0) for v in verdicts),
                       "est_cost_usd": round(cost.cost_usd(model, pt, ot), 6)},
        "examples": [{k: v.get(k) for k in ("question_id", "question", "gold", "pred", "ex", "judge_correct",
                                             "judge_reason")} for v in picked],
    }


def render_md(rep: dict) -> str:
    c = rep["confusion"]
    L = ["# LLM judge vs EX", "",
         f"- Run file: `{rep['run']}`", f"- Judge model: {rep['judge_cost']['model']} "
         f"(provider {rep['provider']})", f"- Command: `{rep['command']}`", f"- Timestamp: {rep['timestamp']}",
         f"- Rows: {rep['n_rows']} (judged {rep['n_judged']}, errors {rep['n_errors']})", "",
         "## Confusion matrix", "", "| | judge correct | judge wrong |", "|---|---|---|",
         f"| EX=1 | {c['ex1_judge_correct']} | {c['ex1_judge_wrong']} |",
         f"| EX=0 | {c['ex0_judge_correct']} | {c['ex0_judge_wrong']} |", "",
         f"- Agreement: {rep['agreement'] * 100:.1f}%", f"- Cohen's kappa: {rep['cohen_kappa']}",
         f"- EX=0 but judge=correct: {rep['disagree_ex0_judge_correct']}",
         f"- EX=1 but judge=wrong: {rep['disagree_ex1_judge_wrong']}",
         f"- Judge cost: ${rep['judge_cost']['est_cost_usd']:.4f} ({rep['judge_cost']['prompt_tokens']} in / "
         f"{rep['judge_cost']['output_tokens']} out tokens, {rep['judge_cost']['llm_calls']} calls; cache hits $0)",
         "", "## Disagreement examples", ""]
    if not rep["examples"]:
        L.append("None.")
    for e in rep["examples"]:
        L += [f"### q{e['question_id']} — EX={e['ex']}, judge={'correct' if e['judge_correct'] else 'wrong'}", "",
              f"- Question: {e['question']}", f"- Judge reason: {e['judge_reason']}", "",
              "Gold:", "```sql", e["gold"] or "", "```", "Pred:", "```sql", e["pred"] or "(empty)", "```", ""]
    return "\n".join(L) + "\n"


def run(run_path: Path, out: Path, model: str, workers: int, limit: int, judge_fn: Callable,
        evidence: dict) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    rows = load_rows(run_path)
    rows = rows[:limit] if limit else rows
    verdicts = run_judge(rows, evidence, out, model, workers, judge_fn)
    rep = {"run": str(run_path), "provider": config.PROVIDER,
           "command": "python -m nlsql.judge_check " + " ".join(sys.argv[1:]),
           "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           **analyze(verdicts, model)}
    (out / "verdicts.json").write_text(json.dumps({"report": rep, "verdicts": verdicts}, indent=2))
    (out / "report.md").write_text(render_md(rep))
    print(f"wrote {out / 'verdicts.json'} and {out / 'report.md'}")
    if rep["n_errors"] * 2 > rep["n_rows"]:
        sys.exit(f"ERROR: {rep['n_errors']}/{rep['n_rows']} judge calls failed; rerun to retry")
    return rep


def main() -> None:
    p = argparse.ArgumentParser(description="LLM judge vs EX agreement on one run file")
    p.add_argument("--run", type=Path, default=None, help="run file (default: tune held-out winner run)")
    p.add_argument("--limit", type=int, default=0, help="0 = all rows")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--model", default=config.JUDGE_MODEL)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = p.parse_args()
    from . import bird, judge

    run_path = a.run or default_run()
    evidence = {e["question_id"]: e.get("evidence", "") for e in bird.load_examples()}
    run(run_path, a.out, a.model, a.workers, a.limit, judge.judge, evidence)


def _selfcheck():
    import tempfile

    rows = [{"question_id": q, "question": f"q{q}", "gold": "SELECT 1", "pred": f"SELECT {q}",
             "ex": int(q % 2 == 0)} for q in range(12)]
    seen = []

    def stub(question, pred_sql, gold_sql, explanation=None, evidence="", model=None):
        q = int(question[1:])
        seen.append((q, evidence))
        if q == 11 and not stub.healed:
            raise RuntimeError("transient")
        return {"correct": q % 3 != 0, "score": 0.5, "reason": f"r{q}"}
    stub.healed = False

    with tempfile.TemporaryDirectory() as td:
        rp = Path(td) / "run.json"
        rp.write_text(json.dumps({"summary": {}, "results": rows}))
        out = Path(td) / "out"
        ev = {q: f"ev{q}" for q in range(12)}
        rep = run(rp, out, "m", 3, 0, stub, ev)
        assert rep["n_rows"] == 12 and rep["n_errors"] == 1 and len(seen) == 12
        assert all(e == f"ev{q}" for q, e in seen)  # evidence looked up by question_id
        # judged q0..q10: EX1 = even; judge correct = q%3 != 0
        c = rep["confusion"]
        assert c == {"ex1_judge_correct": 4, "ex1_judge_wrong": 2, "ex0_judge_correct": 3,
                     "ex0_judge_wrong": 2}, c
        assert rep["agreement"] == round(6 / 11, 4)
        assert [e["question_id"] for e in rep["examples"]] == [1, 0, 5, 6, 7], rep["examples"]
        # resume: only the errored row is re-judged
        stub.healed = True
        seen.clear()
        rep2 = run(rp, out, "m", 3, 0, stub, ev)
        assert [q for q, _ in seen] == [11] and rep2["n_errors"] == 0
        md = (out / "report.md").read_text()
        assert "Cohen's kappa" in md and "EX=0 but judge=correct" in md
        assert len(json.loads((out / "verdicts.json").read_text())["verdicts"]) == 12
        # limit + jsonl input
        jl = Path(td) / "x.rows.jsonl"
        jl.write_text("\n".join(json.dumps(r) for r in rows))
        assert run(jl, Path(td) / "o2", "m", 2, 5, stub, ev)["n_rows"] == 5
    print("judge_check selfcheck ok")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "_selfcheck":
        _selfcheck()
    else:
        main()
