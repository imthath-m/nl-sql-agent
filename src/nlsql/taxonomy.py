"""Failure taxonomy: tag each wrong example by its most likely cause.

Phase-4 eval component. Rule-based and offline — given a run-file result row
(pred / gold SQL, exec status, error strings) it returns exactly one cause tag so a
run can be sliced by failure mode. No DB or API; the LLM-judge (`judge.py`) is a
separate, richer check.

Causes (priority order, first match wins):
  correct           — ex == 1 (not a failure).
  dialect_error     — SQL/error uses a non-SQLite construct (TOP, ILIKE, DATE_TRUNC, ::, ...).
  execution_error   — the prediction failed to run (empty, exec_ok False, or an error string).
  schema_linking    — ran but touches the wrong tables vs gold (missing/extra table).
  wrong_aggregation — same tables, but different aggregation / GROUP BY / DISTINCT.
  join_error        — same tables, but different column=column join keys.
  wrong_filter      — same tables/agg/joins, but different WHERE literals.
  wrong_projection  — same tables/agg/joins/filters, but different selected columns.
  other             — wrong for none of the above detectable reasons.
"""
import re

from . import schema_link

# Constructs that are not valid SQLite (or come from another dialect's gold-writer habits).
_DIALECT = re.compile(
    r"\b(ILIKE|TOP\s+\d|DATE_TRUNC|DATEADD|DATEDIFF|GETDATE|SYSDATE|STRING_AGG|LISTAGG|"
    r"TO_DATE|NULLS\s+(FIRST|LAST)|EXTRACT\s*\(|INTERVAL\s+'|ISNULL\s*\(|CHARINDEX|NVL\s*\()",
    re.IGNORECASE,
)
_DIALECT_ERR = re.compile(
    r"no such function|syntax error|unrecognized token|near \"", re.IGNORECASE
)
_AGG = re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX|GROUP\s+BY|DISTINCT|HAVING)\b", re.IGNORECASE)
_STRING = re.compile(r"'(?:[^']|'')*'")
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")


def _agg_shape(sql: str) -> tuple:
    """Multiset of aggregation markers (COUNT/SUM/.../GROUP BY/DISTINCT/HAVING), sorted."""
    return tuple(sorted(m.group(0).upper().replace("  ", " ") for m in _AGG.finditer(sql or "")))


# column=column equalities (join keys), e.g. T1.id = T2.aid — the alias prefix is dropped.
_EQUIJOIN = re.compile(r"\b(?:\w+\.)?(\w+)\s*=\s*(?:\w+\.)?(\w+)\b")


def _join_keys(sql: str) -> set:
    """Set of column-pair join keys (alias-stripped, order-insensitive)."""
    keys = set()
    for a, b in _EQUIJOIN.findall(sql or ""):
        if not (a.isdigit() or b.isdigit()):  # column=column, not column=literal
            keys.add(frozenset((a.lower(), b.lower())))
    return keys


def _filter_literals(sql: str) -> set:
    """String + numeric literals — a cheap proxy for the WHERE predicate values."""
    s = sql or ""
    lits = {m.group(0) for m in _STRING.finditer(s)}
    lits |= {m.group(0) for m in _NUM.finditer(_STRING.sub("''", s))}
    return lits


def _failed_to_run(row: dict) -> bool:
    pred = (row.get("pred") or "").strip()
    return not pred or row.get("exec_ok") is False or bool(row.get("error"))


def tag_failure(row: dict) -> str:
    """One cause tag for a run-file result row. `correct` when ex == 1."""
    if row.get("ex") == 1:
        return "correct"

    pred, gold = row.get("pred") or "", row.get("gold") or ""
    err = " ".join([row.get("error") or "", *(row.get("repair_errors") or [])])

    if _failed_to_run(row):
        if _DIALECT.search(pred) or _DIALECT_ERR.search(err):
            return "dialect_error"
        return "execution_error"

    # Prediction ran but the result set is wrong — locate the biggest structural gap.
    if _DIALECT.search(pred):
        return "dialect_error"
    if schema_link.tables_in_sql(pred) != schema_link.tables_in_sql(gold):
        return "schema_linking"
    if _agg_shape(pred) != _agg_shape(gold):
        return "wrong_aggregation"
    if _join_keys(pred) != _join_keys(gold):
        return "join_error"
    if _filter_literals(pred) != _filter_literals(gold):
        return "wrong_filter"
    if _projection(pred) != _projection(gold):
        return "wrong_projection"
    return "other"


def _projection(sql: str) -> tuple:
    """Lower-cased SELECT-list tokens between the first SELECT and its FROM."""
    m = re.search(r"\bSELECT\b(.*?)\bFROM\b", sql or "", re.IGNORECASE | re.DOTALL)
    if not m:
        return ()
    cols = [c.strip().lower() for c in m.group(1).split(",")]
    return tuple(sorted(c for c in cols if c))


def histogram(rows: list[dict]) -> dict:
    """{cause: count} over rows, most common first. `correct` included for context."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[tag_failure(r)] = counts.get(tag_failure(r), 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _selfcheck():
    assert tag_failure({"ex": 1}) == "correct"
    assert tag_failure({"ex": 0, "pred": "", "error": "boom"}) == "execution_error"
    assert (
        tag_failure({"ex": 0, "pred": "SELECT TOP 5 x FROM a", "exec_ok": False, "error": "x"})
        == "dialect_error"
    )
    assert (
        tag_failure(
            {"ex": 0, "pred": "SELECT x FROM a", "exec_ok": False, "error": "no such function: X"}
        )
        == "dialect_error"
    )
    # ran but wrong: different tables
    assert (
        tag_failure({"ex": 0, "pred": "SELECT x FROM a", "gold": "SELECT x FROM b", "exec_ok": True})
        == "schema_linking"
    )
    # same tables, different aggregation
    assert (
        tag_failure(
            {
                "ex": 0,
                "pred": "SELECT COUNT(x) FROM a",
                "gold": "SELECT SUM(x) FROM a",
                "exec_ok": True,
            }
        )
        == "wrong_aggregation"
    )
    # same tables/agg, different join key
    assert (
        tag_failure(
            {
                "ex": 0,
                "pred": "SELECT x FROM a JOIN b ON a.id=b.name",
                "gold": "SELECT x FROM a JOIN b ON a.id=b.id",
                "exec_ok": True,
            }
        )
        == "join_error"
    )
    # same structure, different filter value
    assert (
        tag_failure(
            {
                "ex": 0,
                "pred": "SELECT x FROM a WHERE y = 5",
                "gold": "SELECT x FROM a WHERE y = 9",
                "exec_ok": True,
            }
        )
        == "wrong_filter"
    )
    # same structure/filters, different projection
    assert (
        tag_failure(
            {"ex": 0, "pred": "SELECT x FROM a", "gold": "SELECT y FROM a", "exec_ok": True}
        )
        == "wrong_projection"
    )
    hist = histogram([{"ex": 1}, {"ex": 1}, {"ex": 0, "pred": "", "error": "e"}])
    assert hist == {"correct": 2, "execution_error": 1}, hist
    print("taxonomy selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
