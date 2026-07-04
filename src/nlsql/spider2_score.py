"""Spider 2.0-Lite result scorer: compare a predicted result table against the gold CSV.

Phase-7 component. Spider 2.0-Lite does not release runnable gold SQL you can diff by result
set on a local DB — it ships a gold *execution result* CSV per instance and its evaluator
compares your query's output table against that CSV. We mirror that evaluator's semantics
(evaluation_suite/evaluate.py) closely enough to self-score offline:

  - order-insensitive: rows are compared as multisets (each column's values are sorted).
  - column-subset: the gold columns must each be matched by some predicted column; the
    prediction may carry extra columns (Spider2 checks the gold columns are present, not that
    the shapes are identical). Each predicted column is consumed by at most one gold column.
  - float tolerance: numeric cells compare within `tol` (abs or relative); everything else is
    compared as trimmed strings (case-insensitive), with a few normalizations (True/False,
    trailing .0, thousands commas) so CSV text lines up with DB-native types.

`compare` takes two row tables (lists of tuples/lists); `score_csv` reads the gold CSV first.
"""
import csv
import math


def read_csv(path: str) -> tuple[list, list]:
    """(header, rows) from a gold exec_result CSV. rows is a list of string tuples."""
    with open(path, newline="") as f:
        r = list(csv.reader(f))
    if not r:
        return [], []
    return r[0], [tuple(row) for row in r[1:]]


def _num(v):
    """Parse a cell as a float, tolerating $, %, thousands commas; else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "").rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def _norm_str(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v).strip().lower()
    return {"none": "", "null": "", "nan": ""}.get(s, s)


def _col_key(values: list) -> list:
    """Canonical, order-insensitive key for one column's values (numbers before strings)."""
    keyed = []
    for v in values:
        n = _num(v)
        keyed.append((0, n, "") if n is not None else (1, 0.0, _norm_str(v)))
    return sorted(keyed)


def _close(a: float, b: float, tol: float) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


def _cols_match(pred_col: list, gold_col: list, tol: float) -> bool:
    """Do two columns hold the same multiset of values (float-tolerant, order-insensitive)?"""
    if len(pred_col) != len(gold_col):
        return False
    for (pt, pn, ps), (gt, gn, gs) in zip(_col_key(pred_col), _col_key(gold_col)):
        if pt != gt:
            return False
        if pt == 0 and not _close(pn, gn, tol):
            return False
        if pt == 1 and ps != gs:
            return False
    return True


def _columns(rows: list) -> list[list]:
    """Transpose a row table into a list of columns. [] when there are no rows."""
    return [list(c) for c in zip(*rows)] if rows else []


def compare(pred_rows: list, gold_rows: list, tol: float = 1e-2) -> int:
    """1 if every gold column is matched by a distinct predicted column, else 0.

    Order-insensitive over rows; the prediction may have extra columns; numeric cells compare
    within `tol`. An empty gold (no rows and no columns) matches an empty prediction.
    """
    gcols, pcols = _columns(gold_rows), _columns(pred_rows)
    if not gcols:  # gold produced no rows
        return int(not pred_rows)
    if len(pcols) < len(gcols):
        return 0
    used = [False] * len(pcols)
    for gcol in gcols:  # greedy bipartite match: each gold column claims one pred column
        for i, pcol in enumerate(pcols):
            if not used[i] and _cols_match(pcol, gcol, tol):
                used[i] = True
                break
        else:
            return 0
    return 1


def score_csv(pred_rows: list, gold_csv_path: str, tol: float = 1e-2) -> int:
    """Compare predicted rows against a gold exec_result CSV file (header dropped)."""
    _, gold_rows = read_csv(gold_csv_path)
    return compare(pred_rows, gold_rows, tol)


def _selfcheck():
    # Order-insensitive exact match.
    gold = [(1, "a"), (2, "b")]
    assert compare([(2, "b"), (1, "a")], gold) == 1
    # Row mismatch -> 0.
    assert compare([(1, "a"), (3, "b")], gold) == 0
    # Float tolerance.
    assert compare([(1.001, "a"), (2.0, "b")], [(1.0, "a"), (2.0, "b")], tol=1e-2) == 1
    assert compare([(1.5, "a"), (2.0, "b")], [(1.0, "a"), (2.0, "b")], tol=1e-2) == 0
    # Column subset: prediction may carry an extra column the gold doesn't have.
    assert compare([(1, "a", "x"), (2, "b", "y")], gold) == 1
    # Missing a gold column -> 0 (fewer predicted columns than gold).
    assert compare([(1,), (2,)], gold) == 0
    # CSV text vs native types: "1"/"2" strings match ints; commas + $ tolerated.
    assert compare([("1", "a"), ("2", "b")], gold) == 1
    assert compare([(1000,)], [("1,000",)]) == 1
    # Column order does not matter (gold col2 matched by pred col1).
    assert compare([("a", 1), ("b", 2)], gold) == 1
    # Empty gold matches empty prediction only.
    assert compare([], []) == 1 and compare([(1,)], []) == 0

    # score_csv against a written gold file.
    import os, tempfile
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", newline="") as f:
        csv.writer(f).writerows([["n", "label"], ["1", "a"], ["2", "b"]])
    assert score_csv([(2, "b"), (1, "a")], path) == 1
    assert score_csv([(2, "b")], path) == 0
    os.remove(path)
    print("spider2_score selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
