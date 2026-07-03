"""Gate eval: score the abstention gate on a run file — false-refusal and caught-error.

Phase-5 eval component. Consumes a run JSON whose rows recorded the gate's `confidence`
and `abstain` decision per example (see run_phase5) plus the usual `ex`. Offline and
deterministic — no DB or API. Metrics:

  abstention_rate    — share of examples the gate refused.
  false_refusal_rate — of the answers that would have been correct (EX=1), the share the
                       gate wrongly refused. The cost of the gate.
  caught_error_rate  — of the wrong answers (EX=0), the share the gate refused. Its value.
  served_accuracy    — EX among the answers the gate served (did not refuse).

`sweep` recomputes those metrics from the recorded per-row `confidence` across a range of
thresholds, so the operating point can be tuned offline without re-running the pipeline.

    python -m nlsql.gate_eval runs/phase5_<ts>.json
"""
import argparse
import json
from datetime import datetime, timezone

from . import config


def _rate(num: int, den: int) -> float | None:
    """Fraction rounded to 4dp, or None when the denominator is empty (undefined)."""
    return round(num / den, 4) if den else None


def _abstained(row: dict, threshold: float | None) -> bool:
    """Refusal for a row: the recorded decision when threshold is None, else confidence<threshold."""
    if threshold is None:
        return bool(row.get("abstain"))
    c = row.get("confidence")
    return c is not None and c < threshold


def metrics(rows: list[dict], threshold: float | None = None) -> dict:
    """Gate metrics over run rows. threshold=None uses each row's recorded `abstain`;
    a number recomputes refusals from the recorded `confidence` at that threshold."""
    rows = [r for r in rows if r.get("ex") is not None]
    correct = [r for r in rows if r["ex"] == 1]
    wrong = [r for r in rows if r["ex"] == 0]
    abstained = [r for r in rows if _abstained(r, threshold)]
    false_refusals = [r for r in correct if _abstained(r, threshold)]
    caught = [r for r in wrong if _abstained(r, threshold)]
    served = [r for r in rows if not _abstained(r, threshold)]
    return {
        "threshold": threshold,
        "n": len(rows),
        "n_abstained": len(abstained),
        "abstention_rate": _rate(len(abstained), len(rows)),
        "false_refusal_rate": _rate(len(false_refusals), len(correct)),
        "caught_error_rate": _rate(len(caught), len(wrong)),
        "served_accuracy": _rate(sum(r["ex"] for r in served), len(served)),
        "ex_overall": _rate(sum(r["ex"] for r in rows), len(rows)),
    }


def sweep(rows: list[dict], thresholds: list[float] | None = None) -> list[dict]:
    """Metrics recomputed from recorded confidences across a grid of thresholds."""
    if thresholds is None:
        thresholds = [round(0.1 * i, 2) for i in range(11)]  # 0.0 .. 1.0
    return [metrics(rows, t) for t in thresholds]


def evaluate(run: dict) -> dict:
    """Gate report for a loaded run dict: metrics at the recorded decision + a sweep."""
    rows = run.get("results", [])
    return {
        "n": len(rows),
        "threshold": run.get("summary", {}).get("abstain_threshold", config.ABSTAIN_THRESHOLD),
        "at_recorded_decision": metrics(rows),
        "sweep": sweep(rows),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Phase-5 gate eval over a run file")
    p.add_argument("run", help="run JSON with per-row confidence + abstain (from run_phase5)")
    args = p.parse_args()

    run = json.loads(open(args.run).read())
    report = evaluate(run)

    config.RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = config.RUNS_DIR / f"gate_eval_{stamp}.json"
    out.write_text(json.dumps({"run_file": args.run, **report}, indent=2))
    print(json.dumps(report, indent=2))
    print(f"wrote {out}")


def _selfcheck():
    # 4 rows: 2 correct (one refused = a false refusal), 2 wrong (one refused = caught).
    rows = [
        {"question_id": 1, "ex": 1, "confidence": 0.9, "abstain": False},  # correct, served
        {"question_id": 2, "ex": 1, "confidence": 0.3, "abstain": True},   # correct, refused (bad)
        {"question_id": 3, "ex": 0, "confidence": 0.2, "abstain": True},   # wrong, refused (good)
        {"question_id": 4, "ex": 0, "confidence": 0.8, "abstain": False},  # wrong, served (missed)
    ]
    m = metrics(rows)
    assert m["n_abstained"] == 2 and m["abstention_rate"] == 0.5, m
    assert m["false_refusal_rate"] == 0.5, m  # 1 of 2 correct refused
    assert m["caught_error_rate"] == 0.5, m   # 1 of 2 wrong refused
    assert m["served_accuracy"] == 0.5, m     # served rows 1 (ex1) + 4 (ex0) -> 1/2
    assert m["ex_overall"] == 0.5, m

    # Sweep from recorded confidence: threshold above all -> refuse everything.
    top = metrics(rows, threshold=1.0)
    assert top["abstention_rate"] == 1.0 and top["false_refusal_rate"] == 1.0, top
    assert top["caught_error_rate"] == 1.0 and top["served_accuracy"] is None, top
    # threshold below all -> refuse nothing.
    bot = metrics(rows, threshold=0.0)
    assert bot["n_abstained"] == 0 and bot["false_refusal_rate"] == 0.0, bot
    assert bot["served_accuracy"] == 0.5, bot
    # threshold 0.5 refuses confidences 0.3 and 0.2 (rows 2, 3) -> same as recorded here.
    assert metrics(rows, threshold=0.5)["n_abstained"] == 2

    grid = sweep(rows, [0.25, 0.85])
    assert grid[0]["n_abstained"] == 1 and grid[1]["n_abstained"] == 3, grid

    # No correct answers -> false_refusal_rate undefined (None), not a crash.
    none_correct = metrics([{"ex": 0, "confidence": 0.1, "abstain": True}])
    assert none_correct["false_refusal_rate"] is None and none_correct["caught_error_rate"] == 1.0
    print("gate_eval selfcheck ok")


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 2 and sys.argv[1] == "_selfcheck":
        _selfcheck()
    else:
        main()
