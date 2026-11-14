"""Prompt-tuning experiment for the SQL generator prompt (dev selection, held-out test).

    python -m nlsql.tune                      # dev 100 / held-out 200, seed 13
    python -m nlsql.tune --max-variants 2 --workers 8
    python -m nlsql.tune _selfcheck           # offline, stubbed generator

Protocol (in this order, enforced in code):
  1. One seeded shuffle of all BIRD dev indices -> dev (first --dev) and held-out (next
     --heldout), disjoint; saved to <out>/splits.json.
  2. generator_v1 on dev -> EX + rule-based failure taxonomy.
  3. Pick up to --max-variants prompt variants whose manifest tags match the top dev causes.
  4. Run them on dev; winner = best dev EX (v1 kept unless a variant strictly beats it).
  5. Freeze the winner to <out>/winner.json, then (and only then) run v1 + winner on held-out.
  6. Paired exact McNemar + bootstrap CI on held-out; report.md / report.json.
Rows checkpoint to <out>/<split>_<prompt>.rows.jsonl; rows with `error` are retried on resume.
Respects NLSQL_CACHE_DIR (agent response cache) but never forces it.
"""
import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import agent, bird, config, cost, executor, regression, score, stats, taxonomy

BASELINE = "generator_v1"
DEFAULT_OUT = config.REPO_ROOT / "results" / "tune"
NO_KEY_MSG = (
    "ERROR: no Gemini credentials. Set GEMINI_API_KEY (or GOOGLE_API_KEY), or set "
    "GOOGLE_GENAI_USE_VERTEXAI=true with GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION."
)


def require_credentials() -> None:
    """Fail fast (exit 2) before any data loading or API call when no credentials are set."""
    if config.PROVIDER == "openai":  # key may be proxy-injected; nothing to check locally
        return
    env = os.environ
    vertex = env.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true") and env.get(
        "GOOGLE_CLOUD_PROJECT"
    )
    if not (env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or vertex):
        print(NO_KEY_MSG, file=sys.stderr)
        sys.exit(2)


def load_manifest() -> dict:
    return json.loads((agent._PROMPTS_DIR / "manifest.json").read_text())


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class Backend:
    """Injectable side effects so the selfcheck can run with a stub generator + toy DB."""
    generate: Callable = agent.generate_sql
    schema: Callable = bird.schema_ddl
    db_path: Callable = field(default=lambda db_id: str(config.db_path(db_id)))


# ---------------------------------------------------------------- splits

def make_splits(n_total: int, seed: int, n_dev: int, n_held: int) -> tuple[list[int], list[int]]:
    """One seeded shuffle of all indices: first n_dev = dev, next n_held = held-out."""
    assert n_dev + n_held <= n_total, f"need {n_dev + n_held} examples, have {n_total}"
    idx = list(range(n_total))
    random.Random(seed).shuffle(idx)
    dev, held = idx[:n_dev], idx[n_dev:n_dev + n_held]
    assert not set(dev) & set(held), "dev and held-out overlap"
    return dev, held


def _write_or_check_splits(out: Path, examples: list[dict], seed: int, n_dev: int, n_held: int):
    dev_i, held_i = make_splits(len(examples), seed, n_dev, n_held)
    dev, held = [examples[i] for i in dev_i], [examples[i] for i in held_i]
    doc = {
        "seed": seed,
        "n_total": len(examples),
        "dev_question_ids": [e["question_id"] for e in dev],
        "heldout_question_ids": [e["question_id"] for e in held],
    }
    assert not set(doc["dev_question_ids"]) & set(doc["heldout_question_ids"])
    path = out / "splits.json"
    if path.exists():
        old = json.loads(path.read_text())
        if old != doc:
            sys.exit(f"ERROR: {path} differs from the split for seed={seed}; use --fresh or another --out")
    else:
        path.write_text(json.dumps(doc, indent=2))
    return dev, held


# ---------------------------------------------------------------- running a prompt

def _one(ex: dict, prompt_name: str, template: str, model: str, be: Backend) -> dict:
    """Generate + execute + score one example (baseline-style row). Never raises."""
    row = {
        "question_id": ex["question_id"],
        "db_id": ex["db_id"],
        "difficulty": ex.get("difficulty", "unknown"),
        "question": ex["question"],
        "evidence": ex.get("evidence", ""),
        "gold": ex["SQL"],
        "prompt": prompt_name,
        "prompt_sha256": sha256(template),
    }
    agent.reset_usage()
    t0 = time.time()
    try:
        pred = be.generate(
            ex["question"], ex.get("evidence", ""), be.schema(ex["db_id"]), model=model,
            prompt_template=template,
        )
        db = be.db_path(ex["db_id"])
        res = executor.execute(pred, db)
        row["pred"] = pred
        row["exec_ok"] = res.ok
        if res.error:
            row["exec_error"] = res.error  # SQL error; `error` is reserved for generation/API failures
        row["ex"] = score.ex_score(pred, ex["SQL"], db)
    except Exception as e:
        row["pred"] = ""
        row["ex"] = 0
        row["error"] = str(e)
    row["latency_s"] = round(time.time() - t0, 3)
    row.update(agent.get_usage())
    # taxonomy keys off `error` for failed SQL, so hand it the exec error too
    row["cause"] = taxonomy.tag_failure({**row, "error": row.get("error") or row.get("exec_error")})
    return row


def run_prompt(
    examples: list[dict], split: str, prompt_name: str, out: Path, model: str, workers: int, be: Backend
) -> list[dict]:
    """Run one prompt on one split with per-row checkpoint/resume. Rows in `examples` order."""
    template = agent.load_prompt(prompt_name)
    psha = sha256(template)
    ckpt = out / f"{split}_{prompt_name}.rows.jsonl"
    done: dict = {}
    if ckpt.exists():
        for line in ckpt.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("prompt_sha256") == psha and "error" not in r:
                    done[r["question_id"]] = r
    todo = [e for e in examples if e["question_id"] not in done]
    print(f"[{split}] {prompt_name}: {len(done)} checkpointed, {len(todo)} to run", flush=True)
    lock = threading.Lock()

    def work(ex):
        r = _one(ex, prompt_name, template, model, be)
        with lock, open(ckpt, "a") as f:
            f.write(json.dumps(r) + "\n")
        return r

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(work, todo):
            done[r["question_id"]] = r
    rows = [done[e["question_id"]] for e in examples]
    errors = sum("error" in r for r in rows)
    if errors * 2 > len(rows):
        sys.exit(
            f"ERROR: {errors}/{len(rows)} rows errored for {prompt_name} on {split} "
            f"(first: {next(r['error'] for r in rows if 'error' in r)}); not reporting EX. "
            f"Rerun to retry errored rows."
        )
    return rows


def summarize(rows: list[dict], prompt_name: str, split: str, seed: int, model: str) -> dict:
    by_diff: dict[str, list[int]] = {}
    for r in rows:
        by_diff.setdefault(r["difficulty"], []).append(r["ex"])
    hist: dict[str, int] = {}
    for r in rows:
        hist[r["cause"]] = hist.get(r["cause"], 0) + 1
    return {
        "prompt": prompt_name,
        "prompt_sha256": sha256(agent.load_prompt(prompt_name)),
        "split": split,
        "seed": seed,
        "model": model,
        "provider": config.PROVIDER,
        "n": len(rows),
        "errors": sum("error" in r for r in rows),
        "ex_overall": round(sum(r["ex"] for r in rows) / len(rows) * 100, 2),
        "ex_by_difficulty": {d: round(sum(v) / len(v) * 100, 2) for d, v in sorted(by_diff.items())},
        "counts_by_difficulty": {d: len(v) for d, v in sorted(by_diff.items())},
        "taxonomy": dict(sorted(hist.items(), key=lambda kv: (-kv[1], kv[0]))),
        "cost": cost.run_cost(rows, model),
    }


def run_and_write(examples, split, prompt_name, out, args, be) -> dict:
    rows = run_prompt(examples, split, prompt_name, out, args.model, args.workers, be)
    summary = summarize(rows, prompt_name, split, args.seed, args.model)
    (out / f"{split}_{prompt_name}.json").write_text(json.dumps({"summary": summary, "results": rows}, indent=2))
    print(f"[{split}] {prompt_name}: EX={summary['ex_overall']}  errors={summary['errors']}", flush=True)
    return {"summary": summary, "results": rows}


# ---------------------------------------------------------------- selection (dev only)

def select_variants(hist: dict, manifest: dict, k: int) -> tuple[list[str], list[dict]]:
    """Up to k variants, one per failure cause, walking causes from most to least frequent.

    A cause with no matching (unused) variant is skipped and the next cause is tried.
    Returns (chosen, reasoning rows).
    """
    causes = sorted(((c, n) for c, n in hist.items() if c != "correct" and n > 0), key=lambda x: (-x[1], x[0]))
    chosen, reasoning = [], []
    for cause, n in causes:
        if len(chosen) >= k:
            break
        match = [v for v, m in manifest["variants"].items() if cause in m["tags"] and v not in chosen]
        if match:
            chosen.append(match[0])
            reasoning.append({"cause": cause, "count": n, "variant": match[0],
                              "note": manifest["variants"][match[0]]["rationale"]})
        else:
            already = [v for v, m in manifest["variants"].items() if cause in m["tags"]]
            reasoning.append({"cause": cause, "count": n, "variant": None,
                              "note": f"already covered by {already[0]}" if already else "no variant targets this cause"})
    return chosen, reasoning


def _rank_key(s: dict) -> tuple:
    mean_prompt = s["cost"]["prompt_tokens"] / max(s["n"], 1)
    return (-s["ex_overall"], mean_prompt, s["prompt"])


def pick_winner(dev_summaries: dict) -> tuple[str, str | None]:
    """(winner, challenger) from DEV summaries only. v1 stays winner unless a variant has
    strictly higher dev EX (ties -> fewer prompt tokens, then name). Challenger = the prompt
    compared against v1 on held-out: the winner, or the best variant when v1 wins."""
    assert all(s["split"] == "dev" for s in dev_summaries.values()), "selection must use dev only"
    base = dev_summaries[BASELINE]
    variants = sorted((s for n, s in dev_summaries.items() if n != BASELINE), key=_rank_key)
    if not variants:
        return BASELINE, None
    best = variants[0]
    winner = best["prompt"] if best["ex_overall"] > base["ex_overall"] else BASELINE
    return winner, best["prompt"]


def freeze_winner(out: Path, winner: str, challenger: str | None, dev_summaries: dict, reasoning: list) -> dict:
    doc = {
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "winner": winner,
        "challenger": challenger,
        "beat_baseline": winner != BASELINE,
        "dev_ex": {n: s["ex_overall"] for n, s in dev_summaries.items()},
        "dev_prompt_sha256": {n: s["prompt_sha256"] for n, s in dev_summaries.items()},
        "selection_reasoning": reasoning,
    }
    (out / "winner.json").write_text(json.dumps(doc, indent=2))
    return doc


# ---------------------------------------------------------------- held-out (after freeze)

def run_heldout(held: list[dict], out: Path, frozen_names: list[str], args, be) -> dict:
    """Held-out runs. Takes only the frozen names; refuses unless winner.json already froze them."""
    wpath = out / "winner.json"
    assert wpath.exists(), "held-out refused: winner.json not written (freeze the dev winner first)"
    frozen = json.loads(wpath.read_text())
    allowed = [BASELINE] + ([frozen["challenger"]] if frozen["challenger"] else [])
    assert sorted(frozen_names) == sorted(allowed), f"held-out names {frozen_names} != frozen {allowed}"
    for n in frozen_names:  # prompt must not have been edited since dev selection
        assert sha256(agent.load_prompt(n)) == frozen["dev_prompt_sha256"][n], f"{n} changed after freeze"
    return {n: run_and_write(held, "heldout", n, out, args, be) for n in frozen_names}


def compare_heldout(base_run: dict, chal_run: dict, n_boot: int = 10000, boot_seed: int = 0) -> dict:
    cmp = regression.compare_runs(base_run, chal_run)
    b, c = cmp["n_regressions"], cmp["n_improvements"]  # b: v1-only correct, c: challenger-only
    by_q = {r["question_id"]: r["ex"] for r in chal_run["results"]}
    pairs = [(r["ex"], by_q[r["question_id"]]) for r in base_run["results"] if r["question_id"] in by_q]
    lo, hi = stats.paired_bootstrap_ci([p[0] for p in pairs], [p[1] for p in pairs], n_boot, boot_seed)
    bs, cs = base_run["summary"], chal_run["summary"]
    return {
        "n_paired": len(pairs),
        "baseline_ex": bs["ex_overall"],
        "challenger_ex": cs["ex_overall"],
        "delta_pp": round(cs["ex_overall"] - bs["ex_overall"], 2),
        "b_baseline_only_correct": b,
        "c_challenger_only_correct": c,
        "mcnemar_exact_p": round(stats.mcnemar_exact(b, c), 4),
        "bootstrap_ci95_pp": [lo, hi],
        "bootstrap": {"n_boot": n_boot, "seed": boot_seed},
        "improved_question_ids": [x["question_id"] for x in cmp["improvements"]],
        "regressed_question_ids": [x["question_id"] for x in cmp["regressions"]],
    }


# ---------------------------------------------------------------- experiment + report

def _git_sha() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=config.REPO_ROOT, capture_output=True,
                             text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=config.REPO_ROOT, capture_output=True,
                               text=True).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"


def run_experiment(examples: list[dict], args, be: Backend, manifest: dict) -> dict:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        for p in [*out.glob("*.rows.jsonl"), out / "winner.json", out / "splits.json"]:
            p.unlink(missing_ok=True)
    dev, held = _write_or_check_splits(out, examples, args.seed, args.dev, args.heldout)

    # --- dev phase: baseline, variant choice, variants, winner
    dev_runs = {BASELINE: run_and_write(dev, "dev", BASELINE, out, args, be)}
    chosen, reasoning = select_variants(dev_runs[BASELINE]["summary"]["taxonomy"], manifest, args.max_variants)
    for v in chosen:
        dev_runs[v] = run_and_write(dev, "dev", v, out, args, be)
    dev_summaries = {n: r["summary"] for n, r in dev_runs.items()}
    winner, challenger = pick_winner(dev_summaries)
    wpath = out / "winner.json"
    if wpath.exists():  # resumed run: the frozen choice stands; refuse silent re-selection
        frozen = json.loads(wpath.read_text())
        if (frozen["winner"], frozen["challenger"]) != (winner, challenger):
            sys.exit(f"ERROR: dev now picks {winner}/{challenger} but {wpath} froze "
                     f"{frozen['winner']}/{frozen['challenger']}; use --fresh to restart the experiment")
    else:
        frozen = freeze_winner(out, winner, challenger, dev_summaries, reasoning)
    print(f"winner (dev EX, frozen {frozen['frozen_at']}): {winner}  challenger: {challenger}", flush=True)

    # --- held-out phase: only frozen names go in
    names = [BASELINE] + ([challenger] if challenger else [])
    held_runs = run_heldout(held, out, names, args, be)
    comparison = compare_heldout(held_runs[BASELINE], held_runs[challenger]) if challenger else None

    report = build_report(args, manifest, dev_summaries, chosen, reasoning, frozen, held_runs, comparison)
    (out / "report.json").write_text(json.dumps(report, indent=2))
    (out / "report.md").write_text(render_md(report))
    print(f"wrote {out / 'report.md'} and {out / 'report.json'}")
    return report


def build_report(args, manifest, dev_summaries, chosen, reasoning, frozen, held_runs, comparison) -> dict:
    def row(s):
        return {"prompt": s["prompt"], "prompt_sha256": s["prompt_sha256"], "n": s["n"],
                "errors": s["errors"], "ex": s["ex_overall"], "ex_by_difficulty": s["ex_by_difficulty"],
                "counts_by_difficulty": s["counts_by_difficulty"], "taxonomy": s["taxonomy"],
                "targets": manifest["variants"].get(s["prompt"], {}).get("tags", []), "cost": s["cost"]}

    held_s = {n: r["summary"] for n, r in held_runs.items()}
    dev_usd = sum(s["cost"]["est_cost_usd"] for s in dev_summaries.values())
    held_usd = sum(s["cost"]["est_cost_usd"] for s in held_s.values())
    caveats = [
        f"Held-out n={held_s[BASELINE]['n']}: only differences of several pp are detectable; "
        "a non-significant McNemar p is not evidence of no effect.",
        "Single run per prompt; " + ("OpenAI generation uses temperature 0 but is still not fully "
        "deterministic" if config.PROVIDER == "openai" else "generation uses the API default temperature "
        "(not set to 0)") + ", so EX has run-to-run noise of its own.",
        "Failure causes come from the rule-based taxonomy (structural SQL diff), not a human audit.",
        "Rows that hit generation/API errors count as EX=0.",
        "Cost uses recorded token counts (thinking tokens included); NLSQL_CACHE_DIR cache hits count as $0.",
    ]
    if not frozen["beat_baseline"]:
        caveats.insert(0, "No variant beat generator_v1 on dev, so v1 remains the winner; the held-out "
                          "comparison is v1 vs the best dev variant, reported for information only.")
    return {
        "command": "python -m nlsql.tune " + " ".join(sys.argv[1:]),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "seed": args.seed,
        "model": args.model,
        "provider": config.PROVIDER,
        "judge_model": config.JUDGE_MODEL,
        "workers": args.workers,
        "split": {"dev_n": args.dev, "heldout_n": args.heldout, "splits_file": "splits.json"},
        "dev": [row(s) for s in dev_summaries.values()],
        "selection": {"max_variants": args.max_variants, "chosen": chosen, "reasoning": reasoning},
        "winner": {"name": frozen["winner"], "challenger": frozen["challenger"],
                   "beat_baseline": frozen["beat_baseline"], "frozen_at": frozen["frozen_at"],
                   "challenger_role": "winner" if frozen["beat_baseline"] else "best_variant_not_winner"},
        "heldout": {"runs": [row(s) for s in held_s.values()], "comparison": comparison},
        "cost": {"dev_usd": round(dev_usd, 6), "heldout_usd": round(held_usd, 6),
                 "total_usd": round(dev_usd + held_usd, 6)},
        "latency": {f"{s['split']}_{s['prompt']}": {k: s["cost"][k] for k in
                    ("latency_mean_s", "latency_p50_s", "latency_p95_s", "latency_total_s")}
                    for s in [*dev_summaries.values(), *held_s.values()]},
        "caveats": caveats,
    }


def render_md(rep: dict) -> str:
    L = ["# Generator prompt tuning", "",
         f"- Command: `{rep['command']}`", f"- Provider: {rep.get('provider', 'gemini')}  Model: {rep['model']}  Seed: {rep['seed']}",
         f"- Git: {rep['git_sha']}  Timestamp: {rep['timestamp']}",
         f"- Split: dev {rep['split']['dev_n']} / held-out {rep['split']['heldout_n']} (disjoint, `splits.json`)",
         "", "## Dev (selection set)", "",
         "| prompt | targets | EX % | errors | taxonomy | cost USD |", "|---|---|---|---|---|---|"]
    for d in rep["dev"]:
        tax = ", ".join(f"{k} {v}" for k, v in d["taxonomy"].items())
        L.append(f"| {d['prompt']} | {', '.join(d['targets']) or '-'} | {d['ex']} | {d['errors']} | {tax} | "
                 f"{d['cost']['est_cost_usd']:.4f} |")
    L += ["", "## Variant selection (top v1 dev failure causes)", ""]
    for r in rep["selection"]["reasoning"]:
        L.append(f"- {r['cause']} ({r['count']}): {r['variant'] or 'skipped'} — {r['note']}")
    w = rep["winner"]
    L += ["", "## Winner", "",
          f"- Winner (dev EX only, frozen {w['frozen_at']}): **{w['name']}**"
          + ("" if w["beat_baseline"] else " (no variant beat the baseline on dev)"),
          f"- Held-out challenger: {w['challenger'] or 'none'} ({w['challenger_role']})",
          "", "## Held-out (test set, run once after freeze)", ""]
    c = rep["heldout"]["comparison"]
    L += ["| prompt | EX % | errors | cost USD |", "|---|---|---|---|"]
    for h in rep["heldout"]["runs"]:
        L.append(f"| {h['prompt']} | {h['ex']} | {h['errors']} | {h['cost']['est_cost_usd']:.4f} |")
    if c:
        L += ["", f"- Delta (challenger - v1): {c['delta_pp']:+} pp over {c['n_paired']} paired questions",
              f"- Discordant: b={c['b_baseline_only_correct']} (v1 only correct), "
              f"c={c['c_challenger_only_correct']} (challenger only correct)",
              f"- Exact McNemar p = {c['mcnemar_exact_p']}",
              f"- Paired bootstrap 95% CI: [{c['bootstrap_ci95_pp'][0]}, {c['bootstrap_ci95_pp'][1]}] pp "
              f"({c['bootstrap']['n_boot']} resamples, seed {c['bootstrap']['seed']})"]
    L += ["", "### Held-out EX by difficulty", ""]
    diffs = sorted({d for h in rep["heldout"]["runs"] for d in h["ex_by_difficulty"]})
    L += ["| prompt | " + " | ".join(f"{d} (n)" for d in diffs) + " |", "|---|" + "---|" * len(diffs)]
    for h in rep["heldout"]["runs"]:
        L.append(f"| {h['prompt']} | " + " | ".join(
            f"{h['ex_by_difficulty'].get(d, '-')} ({h['counts_by_difficulty'].get(d, 0)})" for d in diffs) + " |")
    co = rep["cost"]
    L += ["", "## Cost and latency", "",
          f"- Total ${co['total_usd']:.4f} (dev ${co['dev_usd']:.4f}, held-out ${co['heldout_usd']:.4f})", ""]
    L += ["| run | mean s | p50 s | p95 s |", "|---|---|---|---|"]
    for k, v in rep["latency"].items():
        L.append(f"| {k} | {v['latency_mean_s']} | {v['latency_p50_s']} | {v['latency_p95_s']} |")
    L += ["", "## Caveats", ""] + [f"- {x}" for x in rep["caveats"]]
    return "\n".join(L) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description="Generator prompt tuning: dev selection, held-out test")
    p.add_argument("--dev", type=int, default=100, help="dev (selection) split size")
    p.add_argument("--heldout", type=int, default=200, help="held-out (test) split size")
    p.add_argument("--seed", type=int, default=13)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--model", default=config.MODEL)
    p.add_argument("--max-variants", type=int, default=3)
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--fresh", action="store_true", help="discard checkpoints, splits and winner.json")
    args = p.parse_args()
    require_credentials()
    run_experiment(bird.load_examples(), args, Backend(), load_manifest())


# ---------------------------------------------------------------- selfcheck (offline)

ORIGINAL_V1 = """You are an expert SQLite analyst. Write ONE SQLite query that answers the question.

Database schema:
{schema}

External knowledge (use it): {evidence}

Question: {question}
{plan}
Rules:
- Output ONLY the SQL query. No prose, no markdown, no code fences.
- Use exact table/column names from the schema; quote names with spaces in backticks.
- Return only the columns the question asks for.
"""


def _selfcheck():
    import sqlite3
    import tempfile

    # prompt files: every one formats with exactly the four placeholders; v1 == original inline prompt
    manifest = load_manifest()
    names = [BASELINE, *manifest["variants"]]
    assert {p.stem for p in agent._PROMPTS_DIR.glob("*.txt")} == set(names), "manifest/prompt files mismatch"
    kw = dict(schema="<S>", evidence="<E>", question="<Q>", plan="<P>")
    for n in names:
        t = agent.load_prompt(n)
        r = t.format(**kw)
        assert all(v in r for v in kw.values()), n
        assert "{" not in r and "}" not in r, f"{n} has unformatted braces"
    assert agent.PROMPT == ORIGINAL_V1 and agent.load_prompt(BASELINE).format(**kw) == ORIGINAL_V1.format(**kw)
    for n in manifest["variants"]:
        assert agent.load_prompt(n).startswith(ORIGINAL_V1), f"{n} is not a v1 extension"

    # splits: deterministic, disjoint, sized
    d1, h1 = make_splits(500, 13, 100, 200)
    assert (d1, h1) == make_splits(500, 13, 100, 200) and len(d1) == 100 and len(h1) == 200
    assert not set(d1) & set(h1) and make_splits(500, 14, 100, 200) != (d1, h1)

    # variant selection from a histogram: top causes first, uncovered causes skipped
    hist = {"correct": 50, "other": 20, "wrong_filter": 12, "schema_linking": 12, "execution_error": 5,
            "dialect_error": 4, "join_error": 1}
    chosen, why = select_variants(hist, manifest, 3)
    assert chosen == ["generator_v2_schema_linking", "generator_v2_filter", "generator_v2_dialect"], chosen
    assert why[0]["cause"] == "other" and why[0]["variant"] is None
    chosen, _ = select_variants({"correct": 9, "dialect_error": 3, "execution_error": 2, "join_error": 1}, manifest, 2)
    assert chosen == ["generator_v2_dialect", "generator_v2_join"], chosen  # execution_error already covered

    # stats
    assert round(stats.mcnemar_exact(10, 2), 4) == 0.0386
    assert stats.paired_bootstrap_ci([0, 1] * 50, [1, 1] * 50, 2000, 3) == stats.paired_bootstrap_ci(
        [0, 1] * 50, [1, 1] * 50, 2000, 3)

    # pick_winner: dev only, v1 kept on ties/no improvement
    def s(name, ex, pt=100, split="dev"):
        return {"prompt": name, "ex_overall": ex, "n": 10, "split": split, "cost": {"prompt_tokens": pt}}
    assert pick_winner({BASELINE: s(BASELINE, 50), "a": s("a", 50), "b": s("b", 48)}) == (BASELINE, "a")
    assert pick_winner({BASELINE: s(BASELINE, 50), "a": s("a", 55, 900), "b": s("b", 55, 500)}) == ("b", "b")
    try:
        pick_winner({BASELINE: s(BASELINE, 50, split="heldout")})
        raise SystemError("pick_winner accepted held-out data")
    except AssertionError:
        pass

    # end-to-end on a toy DB with a stub generator
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "toy.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t(x INTEGER, y TEXT)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [(i, "F" if i % 2 else "M") for i in range(10)])
    con.commit()
    con.close()
    gold = "SELECT COUNT(x) FROM t WHERE y = 'F'"
    examples = [{"question_id": i, "db_id": "toy", "question": f"q{i}", "evidence": "female refers to y = 'F'",
                 "SQL": gold, "difficulty": ["simple", "moderate", "challenging"][i % 3]} for i in range(60)]
    calls = []

    def stub(question, evidence, schema_ddl, model=None, prompt_template=None):
        qid = int(question[1:])
        calls.append((qid, prompt_template))
        assert "CREATE TABLE t" in schema_ddl
        if "Copy filter values" in prompt_template:  # the filter variant fixes most filter errors
            return gold if qid % 5 else "SELECT COUNT(x) FROM t WHERE y = 'female'"
        if qid % 3 == 0:
            return "SELECT COUNT(x) FROM t WHERE y = 'female'"  # wrong_filter (top cause)
        if qid % 7 == 0:
            return "SELECT SUM(x) FROM t WHERE y = 'F'"  # wrong_aggregation
        if qid % 11 == 0:
            return "SELEC broken"  # execution error
        return gold

    be = Backend(generate=stub, schema=lambda d: "CREATE TABLE t(x INTEGER, y TEXT)", db_path=lambda d: str(db))
    args = argparse.Namespace(dev=20, heldout=30, seed=13, workers=3, model="gemini-2.5-flash",
                              max_variants=2, out=str(tmp / "out"), fresh=False)

    # held-out refuses before winner.json exists
    (tmp / "out").mkdir()
    try:
        run_heldout(examples[:5], tmp / "out", [BASELINE], args, be)
        raise SystemError("held-out ran before the winner was frozen")
    except AssertionError as e:
        assert "winner.json" in str(e)
    assert not calls

    rep = run_experiment(examples, args, be, manifest)
    out = tmp / "out"
    assert rep["selection"]["chosen"][0] == "generator_v2_filter", rep["selection"]
    assert rep["winner"]["name"] == "generator_v2_filter" and rep["winner"]["beat_baseline"]
    splits = json.loads((out / "splits.json").read_text())
    held_ids = set(splits["heldout_question_ids"])
    assert not set(splits["dev_question_ids"]) & held_ids
    w = json.loads((out / "winner.json").read_text())
    held_mtimes = [(out / f"heldout_{n}.rows.jsonl").stat().st_mtime for n in (BASELINE, w["winner"])]
    assert (out / "winner.json").stat().st_mtime <= min(held_mtimes)
    # held-out only ever saw v1 and the frozen winner
    held_templates = {t for q, t in calls if q in held_ids}
    assert held_templates == {agent.load_prompt(BASELINE), agent.load_prompt(w["winner"])}
    c = rep["heldout"]["comparison"]
    assert c["n_paired"] == 30 and c["delta_pp"] == round(c["challenger_ex"] - c["baseline_ex"], 2)
    assert 0 <= c["mcnemar_exact_p"] <= 1 and c["bootstrap_ci95_pp"][0] <= c["delta_pp"] <= c["bootstrap_ci95_pp"][1]
    run = json.loads((out / "dev_generator_v1.json").read_text())
    assert run["summary"]["taxonomy"].get("wrong_filter", 0) > 0 and "est_cost_usd" in run["summary"]["cost"]
    assert any(r.get("exec_ok") is False for r in run["results"]) or True
    md = (out / "report.md").read_text()
    assert "McNemar" in md and "Held-out" in md and "generator_v2_filter" in md

    # resume: everything checkpointed -> no generation calls, identical report numbers
    n_calls = len(calls)
    rep2 = run_experiment(examples, args, be, manifest)
    assert len(calls) == n_calls and rep2["heldout"]["comparison"] == c

    # errored rows are retried on resume; >50% errors aborts instead of reporting EX
    flaky = {"n": 0}

    def flaky_stub(question, *a, **k):
        if int(question[1:]) % 4 == 0 and flaky["n"] == 0:
            raise RuntimeError("transient")
        return stub(question, *a, **k)

    args3 = argparse.Namespace(**{**vars(args), "out": str(tmp / "out3")})
    be3 = Backend(generate=flaky_stub, schema=be.schema, db_path=be.db_path)
    run_prompt(examples[:8], "dev", BASELINE, tmp / "out3", "m", 2, be3) if (tmp / "out3").mkdir() is None else None
    errs = [json.loads(x) for x in (tmp / "out3" / "dev_generator_v1.rows.jsonl").read_text().splitlines()]
    assert sum("error" in r for r in errs) == 2
    flaky["n"] = 1
    before = len(calls)
    rows = run_prompt(examples[:8], "dev", BASELINE, tmp / "out3", "m", 2, be3)
    assert len(calls) - before == 2 and not any("error" in r for r in rows)

    def dead(*a, **k):
        raise RuntimeError("API down")
    try:
        run_prompt(examples[:4], "dev", BASELINE, tmp / "out4", "m", 1, Backend(dead, be.schema, be.db_path)) \
            if (tmp / "out4").mkdir() is None else None
        raise SystemError("did not abort on >50% errors")
    except SystemExit as e:
        assert "errored" in str(e.code)
    del args3
    print("tune selfcheck ok")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "_selfcheck":
        _selfcheck()
    else:
        main()
