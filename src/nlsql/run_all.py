"""One-command runner: baseline vs loop vs phase3 on the SAME seeded subset -> results/.

    python -m nlsql.run_all [--n 150] [--seed 0] [--workers 4] [--fresh] [--skip-fetch]
    python -m nlsql.run_all _selfcheck       # offline, no API/data

Per-mode rows are appended to <out>/<mode>.rows.jsonl as they finish, so a re-run resumes
(rows with an `error` are retried; --fresh restarts). Deltas are paired by question_id from
the run files themselves (not runs/scores.log). Summary files land next to --out
(default results/summary.{md,json}).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import agent, config

MODES = ("baseline", "loop", "phase3")
EARLY_ROWS = 20  # abort if >50% of the first 20 finished rows errored


def _has_credentials() -> bool:
    if config.PROVIDER == "openai":  # key may be proxy-injected; nothing to check locally
        return True
    e = os.environ
    return bool(e.get("GEMINI_API_KEY") or e.get("GOOGLE_API_KEY")
                or e.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true"))


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=config.REPO_ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def _load_done(path: Path) -> dict[int, dict]:
    """question_id -> last non-error row already on disk."""
    done: dict[int, dict] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if "error" in r:
                    done.pop(r["question_id"], None)
                else:
                    done[r["question_id"]] = r
    return done


def run_mode(mode: str, fn, subset: list[dict], out: Path, workers: int, fresh: bool) -> list[dict]:
    """Run fn(example)->row over subset with checkpointing; returns rows in subset order."""
    path = out / f"{mode}.rows.jsonl"
    if fresh and path.is_file():
        path.unlink()
    done = _load_done(path)
    todo = [ex for ex in subset if ex["question_id"] not in done]
    print(f"[{mode}] {len(done)} cached, {len(todo)} to run", flush=True)
    seen = errs = 0
    with ThreadPoolExecutor(max_workers=workers) as pool, open(path, "a") as f:
        futs = [pool.submit(fn, ex) for ex in todo]
        for fut in as_completed(futs):
            row = fut.result()
            f.write(json.dumps(row) + "\n")
            f.flush()
            done[row["question_id"]] = row
            seen += 1
            errs += "error" in row
            if seen == EARLY_ROWS and errs > EARLY_ROWS / 2:
                pool.shutdown(wait=False, cancel_futures=True)
                sys.exit(f"[{mode}] ABORT: {errs}/{seen} of the first rows errored "
                         f"(last error: {row.get('error', '')[:200]}). Check the API key/quota.")
            if seen % 10 == 0:
                print(f"[{mode}] {seen}/{len(todo)}", flush=True)
    rows = [done[ex["question_id"]] for ex in subset]
    n_err = sum("error" in r for r in rows)
    if n_err > len(rows) / 2:
        sys.exit(f"[{mode}] ABORT: {n_err}/{len(rows)} rows errored; not writing results.")
    return rows


def _pair(base: list[dict], new: list[dict]) -> dict:
    b = {r["question_id"]: r["ex"] for r in base}
    n = {r["question_id"]: r["ex"] for r in new}
    ids = b.keys() & n.keys()
    fixed = sum(b[q] == 0 and n[q] == 1 for q in ids)
    broken = sum(b[q] == 1 and n[q] == 0 for q in ids)
    return {"fixed": fixed, "broken": broken, "delta_pp": round((fixed - broken) / len(ids) * 100, 2) if ids else 0.0}


def build_summary(runs: dict[str, dict], evals: dict[str, dict], meta: dict) -> dict:
    """runs/evals: mode -> run-file dict / eval report. Every number is derived from them."""
    modes, base = {}, runs.get("baseline")
    for m, run in runs.items():
        rows, s, c = run["results"], run["summary"], evals[m]["cost"]
        top = [[k, v] for k, v in evals[m]["failure_taxonomy"].items() if k != "correct"][:3]
        modes[m] = {
            "n": len(rows), "ex_pct": s["ex_overall"], "ex_by_difficulty": s["ex_by_difficulty"],
            "counts_by_difficulty": s["counts_by_difficulty"],
            "est_cost_usd": c["est_cost_usd"], "cost_per_example_usd": c["cost_per_example_usd"],
            "latency_mean_s": c["latency_mean_s"], "latency_p50_s": c["latency_p50_s"],
            "latency_p95_s": c["latency_p95_s"], "llm_calls": c["llm_calls"],
            "errors": sum("error" in r for r in rows), "top_failures": top,
        }
        if base is not None and m != "baseline":
            modes[m].update(_pair(base["results"], rows))
    return {**meta, "total_cost_usd": round(sum(x["est_cost_usd"] for x in modes.values()), 6), "modes": modes}


def render_md(s: dict) -> str:
    diffs = sorted({d for m in s["modes"].values() for d in m["ex_by_difficulty"]})
    head = ["mode", "n", "EX %"] + [f"EX {d} %" for d in diffs] + [
        "delta vs baseline (pp)", "fixed / broken", "est cost USD", "cost/example USD",
        "latency mean s", "p50 s", "p95 s", "LLM calls", "errors"]
    lines = ["# NL-to-SQL run_all results", "", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for name, m in s["modes"].items():
        delta = f"{m['delta_pp']:+.2f}" if "delta_pp" in m else "-"
        fb = f"{m['fixed']} / {m['broken']}" if "fixed" in m else "-"
        cells = [name, m["n"], m["ex_pct"]] + [m["ex_by_difficulty"].get(d, "-") for d in diffs] + [
            delta, fb, f"{m['est_cost_usd']:.4f}", f"{m['cost_per_example_usd']:.5f}",
            m["latency_mean_s"], m["latency_p50_s"], m["latency_p95_s"], m["llm_calls"], m["errors"]]
        lines.append("| " + " | ".join(str(c) for c in cells) + " |")
    lines += ["", f"Total est. cost: ${s['total_cost_usd']:.4f}", "", "## Top failure causes"]
    for name, m in s["modes"].items():
        lines.append(f"- {name}: " + (", ".join(f"{k} ({v})" for k, v in m["top_failures"]) or "none"))
    lines += ["", "## Provenance",
              f"- Command: `{s['command']}`", f"- Seed: {s['seed']}", f"- Provider: {s.get('provider', 'gemini')}  Model: {s['model']}  Reasoning effort: {s.get('reasoning_effort', 'n/a')}",
              f"- Commit: {s['commit']}", f"- Timestamp (UTC): {s['timestamp']}", "",
              f"Caveats: subset of n={s['n']} BIRD dev examples (seed {s['seed']}); cost is an estimate "
              "from token counts x configured prices; deltas are paired by question_id vs baseline."]
    return "\n".join(lines) + "\n"


def write_all(out: Path, runs: dict, meta: dict) -> dict:
    from . import run_eval
    out.mkdir(parents=True, exist_ok=True)
    evals = {}
    for m, run in runs.items():
        (out / f"{m}.json").write_text(json.dumps(run, indent=2))
        evals[m] = run_eval.evaluate(run)
        (out / f"eval_{m}.json").write_text(json.dumps(evals[m], indent=2))
    summ = build_summary(runs, evals, meta)
    (out.parent / "summary.json").write_text(json.dumps(summ, indent=2))
    (out.parent / "summary.md").write_text(render_md(summ))
    return summ


def main() -> None:
    p = argparse.ArgumentParser(description="Run baseline, loop, phase3 on one seeded subset")
    p.add_argument("--n", type=int, default=150)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--model", default=config.MODEL)
    p.add_argument("--max-repairs", type=int, default=3)
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--out", type=Path, default=config.REPO_ROOT / "results" / "run_all")
    p.add_argument("--skip-fetch", action="store_true")
    p.add_argument("--fresh", action="store_true", help="ignore checkpointed rows")
    a = p.parse_args()

    if not _has_credentials():
        sys.exit("ERROR: no Gemini credentials. Set GEMINI_API_KEY (or GOOGLE_API_KEY, or Vertex: "
                 "GOOGLE_GENAI_USE_VERTEXAI=true + GOOGLE_CLOUD_PROJECT/GOOGLE_CLOUD_LOCATION) and re-run.")
    if not a.skip_fetch:
        from . import fetch_data
        fetch_data.ensure_bird_data()

    from . import bird, run_baseline, run_loop, run_phase3
    subset = bird.smoke_subset(bird.load_examples(), a.n, a.seed)
    a.out.mkdir(parents=True, exist_ok=True)
    fns = {
        "baseline": lambda ex: run_baseline._one(ex, a.model),
        "loop": lambda ex: run_loop._one(ex, a.model, a.max_repairs),
        "phase3": lambda ex: run_phase3._one(ex, a.model, a.max_repairs, a.top_k, True),
    }
    mods = {"baseline": run_baseline, "loop": run_loop, "phase3": run_phase3}
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sha = _git_sha()
    runs = {}
    for m in MODES:
        rows = run_mode(m, fns[m], subset, a.out, a.workers, a.fresh)
        summary = {**mods[m]._summary(rows), "mode": m, "seed": a.seed, "n": len(rows),
                   "question_ids": [r["question_id"] for r in rows], "model": a.model, "provider": config.PROVIDER,
                   "reasoning_effort": agent.reasoning_effort(), "commit": sha, "timestamp": ts}
        runs[m] = {"summary": summary, "results": rows}
    meta = {"command": "python -m nlsql.run_all " + " ".join(sys.argv[1:]), "seed": a.seed, "n": len(subset),
            "model": a.model, "provider": config.PROVIDER, "reasoning_effort": agent.reasoning_effort(),
            "commit": sha, "timestamp": ts}
    write_all(a.out, runs, meta)
    print((a.out.parent / "summary.md").read_text())


def _selfcheck():
    def row(q, ex, diff="simple", err=False):
        r = {"question_id": q, "db_id": "d", "difficulty": diff, "question": "q", "gold": "SELECT 1",
             "pred": "SELECT 1", "ex": ex, "latency_s": 1.0 + q, "prompt_tokens": 1000, "output_tokens": 100,
             "llm_calls": 1}
        if err:
            r.update(error="boom", ex=0, pred="")
        return r

    subset = [{"question_id": q} for q in range(4)]
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "results" / "run_all"
        out.mkdir(parents=True)

        # resume: done rows are skipped, error rows retried, --fresh restarts
        calls = []
        def fn(ex):
            calls.append(ex["question_id"])
            return row(ex["question_id"], 1)
        (out / "x.rows.jsonl").write_text(json.dumps(row(0, 1)) + "\n" + json.dumps(row(1, 0, err=True)) + "\n")
        rows = run_mode("x", fn, subset, out, 2, fresh=False)
        assert sorted(calls) == [1, 2, 3] and [r["question_id"] for r in rows] == [0, 1, 2, 3]
        assert not any("error" in r for r in rows) and set(_load_done(out / "x.rows.jsonl")) == {0, 1, 2, 3}
        calls.clear()
        run_mode("x", fn, subset, out, 2, fresh=True)
        assert sorted(calls) == [0, 1, 2, 3]

        # all-error mode aborts loudly
        try:
            run_mode("bad", lambda ex: row(ex["question_id"], 0, err=True), subset, out, 2, True)
            raise AssertionError("should abort")
        except SystemExit as e:
            assert "ABORT" in str(e)

        # pairing + summary + files
        base = [row(0, 1), row(1, 0, "moderate"), row(2, 0), row(3, 1, "moderate")]
        new = [row(0, 1), row(1, 1, "moderate"), row(2, 1), row(3, 0, "moderate")]
        assert _pair(base, new) == {"fixed": 2, "broken": 1, "delta_pp": 25.0}
        runs = {}
        for m, rs in (("baseline", base), ("loop", new)):
            runs[m] = {"summary": {"n": 4, "ex_overall": sum(r["ex"] for r in rs) * 25.0, "model": "gemini-2.5-flash",
                                   "ex_by_difficulty": {"simple": 50.0}, "counts_by_difficulty": {"simple": 4}},
                       "results": rs}
        meta = {"command": "python -m nlsql.run_all --n 4", "seed": 0, "n": 4, "model": "gemini-2.5-flash",
                "commit": "abc", "timestamp": "t"}
        s = write_all(out, runs, meta)
        assert s["modes"]["loop"]["delta_pp"] == 25.0 and s["modes"]["baseline"]["n"] == 4
        assert s["modes"]["loop"]["est_cost_usd"] > 0
        md = (out.parent / "summary.md").read_text()
        assert "| loop |" in md and "+25.00" in md and "2 / 1" in md and "Caveats" in md
        assert json.loads((out.parent / "summary.json").read_text())["total_cost_usd"] == s["total_cost_usd"]
        assert (out / "eval_loop.json").is_file() and (out / "baseline.json").is_file()
    print("run_all selfcheck ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["_selfcheck"]:
        _selfcheck()
    else:
        main()
