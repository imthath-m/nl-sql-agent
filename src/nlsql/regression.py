"""Regression suite: diff a new run file against a previous one, per example.

Phase-4 eval component. After any change, rerun a run (baseline / loop / phase3 /
their `--limit` smoke subset) and compare its run JSON against a prior run to catch
per-example regressions (was EX=1, now EX=0) — a rising overall score can still hide
new failures. Offline: reads only the run-file `results` rows, keyed by question_id.

    python -m nlsql.regression <old_run.json> <new_run.json>

Exit code is 1 when any regression is found, so it drops into CI / a pre-commit gate.
"""
import argparse
import json
import sys


def _ex_by_id(run: dict) -> dict:
    return {r["question_id"]: r for r in run.get("results", [])}


def compare_runs(old: dict, new: dict) -> dict:
    """Per-example EX diff between two loaded run dicts.

    Compares only the question_ids present in both. Returns regressions (1→0) and
    improvements (0→1) as lists of {question_id, db_id, question}, plus counts and the
    overall EX delta.
    """
    old_rows, new_rows = _ex_by_id(old), _ex_by_id(new)
    shared = old_rows.keys() & new_rows.keys()
    regressions, improvements = [], []
    for qid in sorted(shared):
        o, n = old_rows[qid]["ex"], new_rows[qid]["ex"]
        if o == 1 and n == 0:
            regressions.append(_ref(new_rows[qid]))
        elif o == 0 and n == 1:
            improvements.append(_ref(new_rows[qid]))
    old_ex = old.get("summary", {}).get("ex_overall")
    new_ex = new.get("summary", {}).get("ex_overall")
    return {
        "n_compared": len(shared),
        "only_in_old": sorted(old_rows.keys() - new_rows.keys()),
        "only_in_new": sorted(new_rows.keys() - old_rows.keys()),
        "n_regressions": len(regressions),
        "n_improvements": len(improvements),
        "ex_overall_delta": round(new_ex - old_ex, 2) if old_ex is not None and new_ex is not None else None,
        "regressions": regressions,
        "improvements": improvements,
    }


def _ref(row: dict) -> dict:
    return {k: row.get(k) for k in ("question_id", "db_id", "question")}


def main() -> None:
    p = argparse.ArgumentParser(description="Per-example regression diff of two run files")
    p.add_argument("old", help="baseline run JSON to compare against")
    p.add_argument("new", help="new run JSON to check")
    args = p.parse_args()

    old = json.loads(open(args.old).read())
    new = json.loads(open(args.new).read())
    diff = compare_runs(old, new)

    print(json.dumps({k: v for k, v in diff.items() if k not in ("regressions", "improvements")}, indent=2))
    for r in diff["regressions"]:
        print(f"  REGRESSION  qid={r['question_id']}  db={r['db_id']}  {r['question']}")
    if diff["n_regressions"]:
        print(f"\n{diff['n_regressions']} regression(s) found")
        sys.exit(1)
    print("\nno regressions")


def _selfcheck():
    old = {
        "summary": {"ex_overall": 50.0},
        "results": [
            {"question_id": 1, "db_id": "d", "question": "q1", "ex": 1},
            {"question_id": 2, "db_id": "d", "question": "q2", "ex": 0},
            {"question_id": 3, "db_id": "d", "question": "q3", "ex": 1},
        ],
    }
    new = {
        "summary": {"ex_overall": 50.0},
        "results": [
            {"question_id": 1, "db_id": "d", "question": "q1", "ex": 0},  # regressed
            {"question_id": 2, "db_id": "d", "question": "q2", "ex": 1},  # improved
            {"question_id": 3, "db_id": "d", "question": "q3", "ex": 1},  # unchanged
        ],
    }
    diff = compare_runs(old, new)
    assert diff["n_regressions"] == 1 and diff["regressions"][0]["question_id"] == 1, diff
    assert diff["n_improvements"] == 1 and diff["improvements"][0]["question_id"] == 2, diff
    assert diff["n_compared"] == 3 and diff["ex_overall_delta"] == 0.0, diff
    # non-overlapping ids are reported, not compared
    diff2 = compare_runs(old, {"summary": {}, "results": [{"question_id": 9, "ex": 1}]})
    assert diff2["n_compared"] == 0 and diff2["only_in_new"] == [9], diff2
    print("regression selfcheck ok")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "_selfcheck":
        _selfcheck()
    else:
        main()
